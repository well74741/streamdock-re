"""核对当前源码的免安装包，必须从真实exe父进程检查灯效宿主。

    python -B tools/verify_portable.py [zip路径]

无设备测试：不会启动SDK、不构造用户App，不采集声音；临时目录用完删除。
"""
import ast
import hashlib
import importlib.util
import json
import marshal
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import zipfile

from PyInstaller.archive.readers import CArchiveReader

ROOT = Path(__file__).resolve().parents[1]


def canonical(code):
    def value(item):
        if isinstance(item, types.CodeType): return canonical(item)
        if isinstance(item, tuple): return tuple(value(v) for v in item)
        return item
    return (code.co_code, tuple(value(c) for c in code.co_consts), code.co_names, code.co_varnames,
            code.co_freevars, code.co_cellvars, code.co_flags, code.co_argcount,
            code.co_posonlyargcount, code.co_kwonlyargcount)


def source_code(path):
    return canonical(compile(path.read_text(encoding='utf-8-sig'), str(path), 'exec', dont_inherit=True))


def run_child(args, env):
    child = subprocess.Popen(args, cwd=str(ROOT), env=env)
    try:
        return child.wait(timeout=30)
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=5)


def main():
    tree = ast.parse((ROOT/'app/version.py').read_text(encoding='utf-8-sig'))
    version = next(ast.literal_eval(node.value) for node in tree.body if isinstance(node, ast.Assign)
                   and any(isinstance(t, ast.Name) and t.id == 'VERSION' for t in node.targets))
    source = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else ROOT/f'dist/release/M18Console-Portable-{version}.zip'
    with tempfile.TemporaryDirectory(prefix='m18-package-check-') as temp:
        folder = Path(temp)
        package = folder/'package'
        package.mkdir()
        with zipfile.ZipFile(source) as archive:
            assert archive.testzip() is None, 'ZIP CRC错误'
            names = set(archive.namelist())
            assert 'M18Console.exe' in names and 'mcp/m18-mcp.exe' in names
            for name in ('app.js', 'index.html', 'style.css', 'gif-clock.js', 'effect-editor.js',
                         'effect-host.html', 'effect-pad.js', 'effect-runtime.js', 'effect-worker.js'):
                assert archive.read('_internal/ui/'+name) == (ROOT/'app/ui'/name).read_bytes(), name
            guide = (ROOT/'app/effect_guide.md').read_bytes()
            assert archive.read('_internal/effect_guide.md') == guide
            assert archive.read('mcp/_internal/effect_guide.md') == guide
            assert not any('/myicons/' in n or '/assets/library/' in n or '/assets/uploads/' in n for n in names)
            tracked = subprocess.check_output(['git', 'ls-tree', '-rz', '--name-only', 'HEAD', '--', 'app/presets'], cwd=ROOT)
            for name in (n.decode('utf-8') for n in tracked.split(b'\0') if n):
                assert archive.read('_internal/'+name[4:]) == subprocess.check_output(['git', 'show', 'HEAD:'+name], cwd=ROOT), name
            for name in names:
                assert not Path(name).is_absolute() and '..' not in Path(name).parts, name
            archive.extractall(package)
        print('ZIP、前端资产、灯效文档和默认预设一致；无新增用户素材。')
        exe = CArchiveReader(str(package/'M18Console.exe'))
        pyz = exe.open_embedded_archive(next(n for n in exe.toc if n.endswith('.pyz')))
        for name in ('app_effects', 'app_effect_scenes', 'audio_loopback', 'app_lighting', 'app_media',
                     'app_pages', 'app_render', 'app_scenes', 'app_catalog', 'app_plugins', 'wssdk', 'core'):
            assert canonical(pyz.extract(name)) == source_code(ROOT/'app'/f'{name}.py'), name
        assert canonical(marshal.loads(exe.extract('server'))) == source_code(ROOT/'app/server.py')
        assert 'concurrent.futures.thread' in pyz.toc
        mcp = CArchiveReader(str(package/'mcp/m18-mcp.exe'))
        mcp_pyz = mcp.open_embedded_archive(next(n for n in mcp.toc if n.endswith('.pyz')))
        assert canonical(mcp_pyz.extract('version')) == source_code(ROOT/'app/version.py')
        print('冻结后端/主程序与当前源码一致，版本和运行时依赖存在。')
        env = dict(os.environ, M18_DATA_DIR=str(folder/'data'))
        assert run_child([str(package/'M18Console.exe'), '--help'], env) == 0
        # 必须测试真实exe父进程，不能只在Python中解包执行字节码。
        for label, compat in [('普通启动', False), ('资源管理器兼容层', True)]:
            child_env = {k: v for k, v in env.items() if k.upper() != '__COMPAT_LAYER'}
            if compat: child_env['__COMPAT_LAYER'] = 'DetectorsAppHealth'
            result = folder/('compat.json' if compat else 'normal.json')
            code = run_child([str(package/'M18Console.exe'), '--check-effect-host', str(result)], child_env)
            report = json.loads(result.read_text(encoding='utf-8'))
            assert code == 0 and report['ok'] and report['frozen'] and report['frames'] > 0, report
            assert report['parentCompatibilityLayer'] == compat, report
            print(f'{label}：真实免安装宿主启动/Worker产帧通过，父环境未改，无设备接管。')
        calls = [('initialize', {'protocolVersion':'2024-11-05', 'capabilities':{}, 'clientInfo':{'name':'package-check', 'version':'1'}}),
                 ('tools/list', {}), ('tools/call', {'name':'m18_effect_api', 'arguments':{}})]
        messages = '\n'.join(json.dumps({'jsonrpc':'2.0', 'id':i, 'method':method, 'params':args})
                              for i, (method, args) in enumerate(calls, 1))+'\n'
        child = subprocess.Popen([str(package/'mcp/m18-mcp.exe')], cwd=str(ROOT), env=env,
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True, encoding='utf-8')
        try:
            output, errors = child.communicate(messages, timeout=20)
            assert child.returncode == 0, (child.returncode, errors)
        finally:
            if child.poll() is None: child.terminate(); child.wait(timeout=5)
        replies = [json.loads(line) for line in output.splitlines()]
        assert len(replies) == 3 and not any('error' in r or r.get('result', {}).get('isError') for r in replies)
        assert replies[0]['result']['serverInfo']['version'] == version
        tools = {tool['name']:tool for tool in replies[1]['result']['tools']}
        assert tools['m18_effect_input']['inputSchema']['properties']['key']['maximum'] == 18
        assert 'M18FX' in replies[2]['result']['content'][0]['text']
        print(f'MCP {version}、{len(tools)}个工具、灯效文档读取通过。')
        spec = importlib.util.spec_from_file_location('package_gif_test', ROOT/'tools/test_gif_clock.py')
        test = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(test)
        test.UI = package/'_internal/ui'
        test.main()
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    source.with_name(source.name+'.sha256').write_text(digest+'  '+source.name+'\n', encoding='ascii')
    print('SHA256', digest)
    print('验证完成', source, round(source.stat().st_size/1048576, 1), 'MB')


if __name__ == '__main__': main()
