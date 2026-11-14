// Stream Dock M18 控制台 —— 安装 / 卸载程序（原生 .NET Framework 4，Windows 10/11 自带，无需解压到临时目录）
//
// 由 packaging/build.py 用 Windows 自带的 csc.exe 编译；程序本体 payload.zip 直接附加在 exe 末尾：
//     [Setup.exe 本体][payload.zip][8 字节 payload 长度][8 字节标记 "M18PAYLD"]
// 安装时直接从自身读出 zip 解压到安装目录——不往 %TEMP% 写任何东西。
// 以前用 PyInstaller 单文件打包，启动要先解压到临时目录，被部分电脑的安全软件拦截（Could not create temporary directory）。
//
// 用法：
//   双击                                   图形界面安装
//   --silent [--dir 目录] [--no-shortcut] [--no-launch]   静默安装
//   --uninstall [--silent] [--purge]       卸载（安装目录里的 uninstall.exe 就是本程序去掉载荷后的副本，双击即卸载）
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.IO.Compression;
using System.Management;
using System.Net;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Windows.Forms;
using Microsoft.Win32;

[assembly: AssemblyTitle("Stream Dock M18 控制台 安装程序")]
[assembly: AssemblyProduct("Stream Dock M18 控制台")]
[assembly: AssemblyVersion("1.0.0.0")]

static class M18Setup
{
    const string APP_NAME = "Stream Dock M18 控制台";
    const string VERSION = "__VERSION__";            // build.py 编译前替换
    const string EXE_NAME = "M18Console.exe";
    const string REG_KEY = @"Software\Microsoft\Windows\CurrentVersion\Uninstall\M18Console";
    static readonly byte[] MAGIC = Encoding.ASCII.GetBytes("M18PAYLD");

    static string LocalAppData { get { return Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData); } }
    static string RoamingAppData { get { return Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData); } }
    static string DefaultDir { get { return Path.Combine(LocalAppData, @"Programs\M18 Console"); } }
    static string DataDir { get { return Path.Combine(RoamingAppData, "M18Console"); } }

    // 有些电脑（尤其是开了「受控文件夹访问」的）不许未签名程序写 C:\Users\…，
    // 但同一块盘的其他位置照样能写——所以安装包放哪块盘，就把那块盘的根目录也当候选。
    static string DriveDir()
    {
        try
        {
            string exe = Assembly.GetExecutingAssembly().Location;
            string root = Path.GetPathRoot(exe);
            if (string.IsNullOrEmpty(root)) return null;
            string cand = Path.Combine(root, "M18 Console");
            return string.Equals(Path.GetFullPath(cand), Path.GetFullPath(DefaultDir), StringComparison.OrdinalIgnoreCase) ? null : cand;
        }
        catch { return null; }
    }

    // 最保险的候选：安装包所在文件夹旁边。用户是从这里双击的，说明这个位置一定能写。
    static string ExeNearDir()
    {
        try
        {
            string exe = Assembly.GetExecutingAssembly().Location;
            string dir = Path.GetDirectoryName(exe);
            if (string.IsNullOrEmpty(dir)) return null;
            return Path.Combine(dir, "M18 Console");
        }
        catch { return null; }
    }

    static List<string> InstallCandidates()
    {
        var list = new List<string>();
        Action<string> add = delegate(string path)
        {
            if (string.IsNullOrEmpty(path)) return;
            try
            {
                string full = Path.GetFullPath(path);
                foreach (string other in list)
                    if (string.Equals(Path.GetFullPath(other), full, StringComparison.OrdinalIgnoreCase)) return;
                list.Add(path);
            }
            catch { }
        };
        add(Environment.GetEnvironmentVariable("M18_INSTALL_DIR"));
        add(DefaultDir);
        add(ExeNearDir());
        try { add(Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), "M18 Console")); } catch { }
        add(DriveDir());
        return list;
    }

    // 装不上时最怕"只有一句拒绝访问"：记下当前步骤 + 写日志，出问题能直接看出卡在哪一步、哪个文件
    static string g_step = "启动";
    static readonly List<string> g_warnings = new List<string>();

    static void Step(string s)
    {
        g_step = s;
        Log("· " + s);
    }

    // 日志可能一个位置都写不进去（安全软件整套锁死），所以按顺序试三个地方，写成功哪个用哪个
    static string g_logPath;

    static string[] LogCandidates()
    {
        var list = new List<string>();
        try { list.Add(Path.Combine(Path.Combine(LocalAppData, "M18Console"), "install.log")); } catch { }
        try { list.Add(Path.Combine(Path.GetDirectoryName(Assembly.GetExecutingAssembly().Location), "install.log")); } catch { }
        try { list.Add(Path.Combine(Path.GetTempPath(), "M18Console-install.log")); } catch { }
        return list.ToArray();
    }

    static bool TryAppend(string p, string text)
    {
        try
        {
            string dir = Path.GetDirectoryName(p);
            if (!string.IsNullOrEmpty(dir) && !Directory.Exists(dir)) Directory.CreateDirectory(dir);
            if (File.Exists(p) && new FileInfo(p).Length > 512 * 1024) File.Delete(p);
            File.AppendAllText(p, text, Encoding.UTF8);
            return true;
        }
        catch { return false; }
    }

    // 真写一次才知道能不能写；CreateDirectory 成功不代表能建文件（受控文件夹访问只拦写文件）
    static string LogPath()
    {
        if (!string.IsNullOrEmpty(g_logPath)) return g_logPath;
        foreach (var p in LogCandidates())
            if (TryAppend(p, "")) { g_logPath = p; return p; }
        return null;
    }

    static void Log(string msg)
    {
        string line = DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + "  " + msg + "\r\n";
        if (!string.IsNullOrEmpty(g_logPath) && TryAppend(g_logPath, line)) return;
        g_logPath = null;
        foreach (var p in LogCandidates())
            if (TryAppend(p, line)) { g_logPath = p; return; }
    }

    static string Short(Exception ex, int max)
    {
        string m = ex == null ? "" : ex.GetBaseException().Message.Replace("\r", " ").Replace("\n", " ");
        return m.Length <= max ? m : m.Substring(0, max) + "…";
    }

    static void Warn(string msg, Exception ex)
    {
        string line = ex == null ? msg : msg + "（" + Short(ex, 160) + "）";
        g_warnings.Add(line);
        Log("! " + line);
        if (ex != null) Log("  " + ex.GetType().Name + ": " + ex.Message);
    }

    // 装之前先看看这个位置能不能写：不能写就当场说清楚并给出能写的位置，别等解压到一半才报错
    // 注意：只判断"能不能新建并写入"。「删掉探测文件」失败不算——安全软件刚扫过会短暂占用新文件，
    // 以前把删除失败也当成"写不进去"，结果每台装了杀毒软件的电脑都被判成哪里都写不了。
    static bool CanWriteUnder(string dir, out string why)
    {
        why = "";
        string probeDir;
        try
        {
            dir = Path.GetFullPath(dir.Trim());
            probeDir = dir;
            int guard = 0;
            while (!Directory.Exists(probeDir) && guard++ < 64)
            {
                string parent = Path.GetDirectoryName(probeDir);
                if (string.IsNullOrEmpty(parent) || string.Equals(parent, probeDir, StringComparison.OrdinalIgnoreCase))
                {
                    why = "找不到存在的上级目录";
                    return false;
                }
                probeDir = parent;
            }
        }
        catch (Exception ex)
        {
            why = Short(ex, 140);
            Log("× 路径无效 " + dir + "：" + why);
            return false;
        }

        string probe = Path.Combine(probeDir, ".m18-probe-" + Guid.NewGuid().ToString("N").Substring(0, 8));
        try
        {
            using (var s = new FileStream(probe, FileMode.CreateNew, FileAccess.Write, FileShare.ReadWrite)) s.WriteByte(0);
        }
        catch (Exception ex)
        {
            why = Short(ex, 140);
            Log("× 不能写 " + dir + "（探测 " + probeDir + "）：" + why);
            return false;
        }
        try { File.Delete(probe); } catch { }      // 删不掉无所谓，写进去了就算能写
        Log("√ 可以写 " + dir);
        return true;
    }

    static bool CanWriteUnder(string dir)
    {
        string ignored;
        return CanWriteUnder(dir, out ignored);
    }

    static string FirstWritable(IEnumerable<string> candidates, List<string> report)
    {
        foreach (var c in candidates)
        {
            if (string.IsNullOrEmpty(c)) continue;
            string why;
            bool ok = CanWriteUnder(c, out why);
            if (report != null) report.Add((ok ? "√ " : "× ") + c + (ok ? "" : "  —— " + why));
            if (ok) return Path.GetFullPath(c.Trim());
        }
        return null;
    }

    static string FirstWritable(IEnumerable<string> candidates)
    {
        return FirstWritable(candidates, null);
    }

    // 真正开始装之前再试写一次：权限问题在这一步就暴露，报错里带的是写不进去的那个目录
    static void ProbeWritable(string dir)
    {
        string probe = Path.Combine(dir, ".m18-write-test");
        try
        {
            File.WriteAllBytes(probe, new byte[] { 0 });
        }
        finally
        {
            try { if (File.Exists(probe)) File.Delete(probe); } catch { }
        }
    }

    [DllImport("user32.dll")] static extern bool SetProcessDPIAware();

    [STAThread]
    static int Main(string[] argv)
    {
        var args = new List<string>(argv);
        bool silent = args.Contains("--silent");
        try
        {
            Log("=== " + APP_NAME + " 安装程序 " + VERSION + " 启动 ===");
            Log("参数：" + string.Join(" ", argv));
            Log("用户：" + Environment.UserName + "；LocalAppData：" + LocalAppData);
            Log("程序位置：" + Assembly.GetExecutingAssembly().Location);
            // 排查用：只测某个目录能不能写，不安装
            if (args.Contains("--probe"))
            {
                int pi = args.IndexOf("--probe");
                string pd = (pi >= 0 && pi + 1 < args.Count) ? args[pi + 1] : DefaultDir;
                string pwhy;
                bool pok = CanWriteUnder(pd, out pwhy);
                Console.Error.WriteLine((pok ? "可写：" : "不可写：") + pd + (pok ? "" : "  —— " + pwhy));
                Log("探针 " + (pok ? "可写 " : "不可写 ") + pd + (pok ? "" : "：" + pwhy));
                Environment.Exit(pok ? 0 : 1);
            }
            long payloadLen;
            long payloadPos = FindPayload(out payloadLen);
            if (args.Contains("--uninstall") || payloadPos < 0)
            {
                return Uninstall(silent, args.Contains("--purge")) ? 0 : 1;
            }
            // 只补建快捷方式 + 卸载登记，不重新解压：装好了却不在「开始菜单→所有应用」里时用
            if (args.Contains("--shortcut-only"))
            {
                int si = args.IndexOf("--dir");
                string dir = (si >= 0 && si + 1 < args.Count) ? args[si + 1] : (InstalledDir() ?? DefaultDir);
                string exe2 = Path.Combine(dir, EXE_NAME);
                if (!File.Exists(exe2))
                {
                    Console.Error.WriteLine("找不到程序：" + exe2 + " —— 先正常装一次，或用 --dir 指定安装目录");
                    return 1;
                }
                bool made = false;
                try { MakeShortcut(StartMenuLnk(), exe2, dir); made = true; }
                catch (Exception ex) { Console.Error.WriteLine("× 开始菜单快捷方式没建成：" + Short(ex, 160)); }
                try { MakeShortcut(DesktopLnk(), exe2, dir); made = true; }
                catch (Exception ex) { Console.Error.WriteLine("× 桌面快捷方式没建成：" + Short(ex, 160)); }
                NotifyShell(Path.GetDirectoryName(StartMenuLnk()));
                NotifyShell(Path.GetDirectoryName(DesktopLnk()));
                try
                {
                    using (var k = Registry.CurrentUser.CreateSubKey(REG_KEY))
                    {
                        k.SetValue("DisplayName", APP_NAME);
                        k.SetValue("DisplayVersion", VERSION);
                        k.SetValue("Publisher", "M18 Console");
                        k.SetValue("DisplayIcon", "\"" + exe2 + "\",0");
                        k.SetValue("InstallLocation", dir);
                        k.SetValue("UninstallString", "\"" + Path.Combine(dir, "uninstall.exe") + "\" --uninstall");
                        k.SetValue("NoModify", 1, RegistryValueKind.DWord);
                        k.SetValue("NoRepair", 1, RegistryValueKind.DWord);
                    }
                }
                catch (Exception ex) { Console.Error.WriteLine("警告：卸载信息没登记上（不影响使用）：" + Short(ex, 120)); }
                if (!made) return 3;
                Console.WriteLine("{\"ok\": true, \"lnk\": \"" + StartMenuLnk().Replace("\\", "\\\\") + "\"}");
                return 0;
            }
            if (silent)
            {
                int i = args.IndexOf("--dir");
                string target = (i >= 0 && i + 1 < args.Count) ? args[i + 1] : DefaultDir;
                if (!CanWriteUnder(target))
                {
                    var cand = InstallCandidates();
                    string alt = FirstWritable(cand);
                    if (alt != null && !string.Equals(Path.GetFullPath(alt), Path.GetFullPath(target.Trim()), StringComparison.OrdinalIgnoreCase))
                    {
                        Console.Error.WriteLine("警告：写不进 " + target + "，改装到 " + alt);
                        target = alt;
                    }
                }
                string exe = Install(target, !args.Contains("--no-shortcut"), payloadPos, payloadLen, null);
                foreach (var w in Warnings) Console.Error.WriteLine("警告：" + w);
                if (!args.Contains("--no-launch")) Launch(exe);
                Console.WriteLine("{\"ok\": true, \"exe\": \"" + exe.Replace("\\", "\\\\") + "\"}");
                return 0;
            }
            try { SetProcessDPIAware(); } catch { }
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            Application.Run(new InstallForm(payloadPos, payloadLen));
            return 0;
        }
        catch (Exception ex)
        {
            Log("失败：" + g_step + " / " + ex);
            if (silent) { Console.Error.WriteLine("安装失败：" + Explain(ex)); return 2; }
            MessageBox.Show("出错了：" + Explain(ex), APP_NAME, MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 2;
        }
    }

    // ------------------------------------------------------------------ 被拦截时说清楚原因
    static bool IsAccessDenied(Exception ex)
    {
        for (var e = ex; e != null; e = e.InnerException)
        {
            if (e is UnauthorizedAccessException) return true;
            if (e is IOException && (Marshal.GetHRForException(e) & 0xFFFF) == 5) return true;
            // 快捷方式 / 注册表走 COM，被拦时报的是 0x80070005（E_ACCESSDENIED）
            if (e is COMException && (uint)((COMException)e).HResult == 0x80070005) return true;
        }
        return false;
    }

    // Windows 安全中心「受控文件夹访问」：0 关、1 开、2 审核；查不到返回 -1
    static int ControlledFolderAccess()
    {
        try
        {
            using (var q = new ManagementObjectSearcher(@"root\Microsoft\Windows\Defender",
                                                        "SELECT EnableControlledFolderAccess FROM MSFT_MpPreference"))
            {
                foreach (ManagementObject o in q.Get()) return Convert.ToInt32(o["EnableControlledFolderAccess"]);
            }
        }
        catch { }
        return -1;
    }

    public static string Explain(Exception ex)
    {
        string detail = Short(ex, 400);
        string where = "失败步骤：" + g_step;
        string logPath = LogPath();
        string logLine = logPath == null
            ? "\n\n日志：三个位置都写不进去（%LOCALAPPDATA%、程序旁边、%TEMP%），日志功能不可用"
            : "\n\n详细日志：" + logPath;
        if (!IsAccessDenied(ex))
        {
            return detail + "\n\n" + where + logLine;
        }
        string self = Assembly.GetExecutingAssembly().Location;
        string why = ControlledFolderAccess() == 1
            ? "这台电脑开启了 Windows 安全中心的「受控文件夹访问」（勒索软件防护），它会阻止未签名的程序写文件。"
            : "这台电脑的安全软件（或 Windows 安全中心）阻止了安装程序写文件——换任何文件夹都会被拒绝。";
        return detail + "\n\n" + where + "\n\n" + why + "\n\n解决办法（任选其一）：\n"
            + "1. Windows 安全中心 → 病毒和威胁防护 → 勒索软件防护 → 管理勒索软件防护 →\n"
            + "   「通过受控文件夹访问允许应用」，添加本安装程序：\n   " + self + "\n"
            + "   （装好后对安装目录里的 M18Console.exe 也做一次）\n"
            + "2. 在你装的安全软件（360 / 火绒 / 卡巴斯基等）里把安装程序和 M18Console.exe 加入信任\n"
            + "3. 运行源码里的「诊断.bat」：自动查出是谁拦截，并可一键放行\n"
            + "4. 这台电脑上改用源码版（双击「启动 M18 控制台.bat」）" + logLine;
    }

    // ------------------------------------------------------------------ 载荷
    static long FindPayload(out long length)
    {
        length = 0;
        string self = Assembly.GetExecutingAssembly().Location;
        using (var fs = new FileStream(self, FileMode.Open, FileAccess.Read, FileShare.ReadWrite))
        {
            if (fs.Length < 32) return -1;
            var tail = new byte[16];
            fs.Seek(-16, SeekOrigin.End);
            fs.Read(tail, 0, 16);
            for (int i = 0; i < 8; i++) if (tail[8 + i] != MAGIC[i]) return -1;
            length = BitConverter.ToInt64(tail, 0);
            long pos = fs.Length - 16 - length;
            return (length > 0 && pos > 0) ? pos : -1;
        }
    }

    // ------------------------------------------------------------------ 安装
    // 除了「把文件写进安装目录」这一步，其余（快捷方式 / 注册表 / 卸载程序）装不上都只算警告：
    // 以前任何一处失败都会让整个安装报"失败"，用户以为没装上，其实程序已经在目录里了。
    public static IList<string> Warnings { get { return g_warnings; } }

    public static string Install(string target, bool desktop, long payloadPos, long payloadLen, Action<int, string> progress)
    {
        if (progress == null) progress = delegate { };
        g_warnings.Clear();
        target = Path.GetFullPath(target.Trim());

        Step("关闭正在运行的控制台");
        progress(3, "正在关闭运行中的控制台…");
        StopRunning();

        Step("创建安装目录：" + target);
        progress(5, "正在创建安装目录…");
        Directory.CreateDirectory(target);
        ProbeWritable(target);          // 先行试探，权限问题在这里就报出准确路径

        string self = Assembly.GetExecutingAssembly().Location;
        string rootFull = target.TrimEnd('\\') + "\\";
        using (var fs = new FileStream(self, FileMode.Open, FileAccess.Read, FileShare.ReadWrite))
        {
            Step("读取安装包内容");
            var buf = new byte[payloadLen];
            fs.Seek(payloadPos, SeekOrigin.Begin);
            int read = 0;
            while (read < buf.Length)
            {
                int n = fs.Read(buf, read, buf.Length - read);
                if (n <= 0) throw new IOException("安装包不完整（可能没下载完），请重新下载");
                read += n;
            }
            using (var zip = new ZipArchive(new MemoryStream(buf), ZipArchiveMode.Read))
            {
                int total = zip.Entries.Count, i = 0;
                foreach (var entry in zip.Entries)
                {
                    i++;
                    string dest = Path.GetFullPath(Path.Combine(target, entry.FullName.Replace('/', '\\')));
                    if (!dest.StartsWith(rootFull, StringComparison.OrdinalIgnoreCase)) continue;
                    g_step = "解压到：" + dest;
                    try
                    {
                        if (entry.FullName.EndsWith("/")) { Directory.CreateDirectory(dest); continue; }
                        Directory.CreateDirectory(Path.GetDirectoryName(dest));
                        // 覆盖旧文件前先去掉只读属性，否则会被当成"拒绝访问"
                        try { if (File.Exists(dest)) File.SetAttributes(dest, FileAttributes.Normal); } catch { }
                    }
                    catch (Exception ex)
                    {
                        if (!(ex is IOException || ex is UnauthorizedAccessException)) throw;
                        throw new IOException("无法写入 " + dest + "：" + Short(ex, 200), ex);
                    }
                    for (int attempt = 0; ; attempt++)
                    {
                        try { entry.ExtractToFile(dest, true); break; }
                        catch (IOException) { if (attempt >= 5) throw; Thread.Sleep(1000); }
                        catch (UnauthorizedAccessException) { if (attempt >= 5) throw; Thread.Sleep(1000); }
                    }
                    if (i % 25 == 0) progress(5 + 75 * i / Math.Max(1, total), "正在复制文件…（" + i + "/" + total + "）");
                }
            }
            // 卸载程序 = 本程序去掉载荷的部分（几十 KB）
            Step("写入卸载程序");
            progress(82, "正在写入卸载程序…");
            var head = new byte[payloadPos];
            fs.Seek(0, SeekOrigin.Begin);
            int got = 0;
            while (got < head.Length) { int n = fs.Read(head, got, head.Length - got); if (n <= 0) break; got += n; }
            string uninstallPath = Path.Combine(target, "uninstall.exe");
            try
            {
                try { if (File.Exists(uninstallPath)) File.SetAttributes(uninstallPath, FileAttributes.Normal); } catch { }
                File.WriteAllBytes(uninstallPath, head);
            }
            catch (Exception ex)
            {
                Warn("卸载程序没写进去（「应用和功能」里不会出现卸载项，可以删安装目录来卸载）", ex);
            }
        }
        string exe = Path.Combine(target, EXE_NAME);
        string uninstaller = Path.Combine(target, "uninstall.exe");
        progress(88, "正在创建快捷方式…");
        Step("创建开始菜单快捷方式");
        try { MakeShortcut(StartMenuLnk(), exe, target); }
        catch (Exception ex) { Warn("开始菜单快捷方式没建成（可从 " + target + " 里直接运行 M18Console.exe）", ex); }
        if (desktop)
        {
            Step("创建桌面快捷方式");
            try { MakeShortcut(DesktopLnk(), exe, target); }
            catch (Exception ex) { Warn("桌面快捷方式没建成（可从 " + target + " 里直接运行 M18Console.exe）", ex); }
        }
        Step("登记卸载信息");
        progress(94, "正在登记卸载信息…");
        try
        {
            using (var k = Registry.CurrentUser.CreateSubKey(REG_KEY))
            {
                k.SetValue("DisplayName", APP_NAME);
                k.SetValue("DisplayVersion", VERSION);
                k.SetValue("Publisher", "M18 Console");
                k.SetValue("DisplayIcon", "\"" + exe + "\",0");
                k.SetValue("InstallLocation", target);
                k.SetValue("UninstallString", "\"" + uninstaller + "\" --uninstall");
                k.SetValue("QuietUninstallString", "\"" + uninstaller + "\" --uninstall --silent");
                k.SetValue("NoModify", 1, RegistryValueKind.DWord);
                k.SetValue("NoRepair", 1, RegistryValueKind.DWord);
                k.SetValue("EstimatedSize", (int)(DirSize(target) / 1024), RegistryValueKind.DWord);
            }
        }
        catch (Exception ex)
        {
            Warn("卸载信息没登记上（不影响使用，卸载时删安装目录即可）", ex);
        }
        Step("完成");
        // 让开始菜单/桌面立即刷新，新装的条目马出现在「所有应用」里
        try { NotifyShell(Path.GetDirectoryName(StartMenuLnk())); } catch { }
        try { NotifyShell(Path.GetDirectoryName(DesktopLnk())); } catch { }
        progress(100, "安装完成");
        return exe;
    }

    public static void Launch(string exe)
    {
        try { Process.Start(new ProcessStartInfo(exe) { WorkingDirectory = Path.GetDirectoryName(exe), UseShellExecute = true }); }
        catch { }
    }

    public static string InstalledDir()
    {
        try
        {
            using (var k = Registry.CurrentUser.OpenSubKey(REG_KEY))
            {
                return k == null ? null : k.GetValue("InstallLocation") as string;
            }
        }
        catch { return null; }
    }

    static long DirSize(string dir)
    {
        long total = 0;
        try { foreach (var f in Directory.GetFiles(dir, "*", SearchOption.AllDirectories)) { try { total += new FileInfo(f).Length; } catch { } } }
        catch { }
        return total;
    }

    // ------------------------------------------------------------------ 快捷方式
    static string StartMenuLnk() { return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.Programs), APP_NAME + ".lnk"); }
    static string DesktopLnk() { return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory), APP_NAME + ".lnk"); }
    // 控制台里开过「开机自启」（安装版用这个名字；源码版是「…（源码版）.lnk」，卸载不碰）
    static string StartupLnk() { return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.Startup), APP_NAME + ".lnk"); }

    static void MakeShortcut(string lnk, string target, string workdir)
    {
        string dir = Path.GetDirectoryName(lnk);
        if (!string.IsNullOrEmpty(dir)) Directory.CreateDirectory(dir);
        Type t = Type.GetTypeFromProgID("WScript.Shell");
        object shell = Activator.CreateInstance(t);
        object sc = t.InvokeMember("CreateShortcut", BindingFlags.InvokeMethod, null, shell, new object[] { lnk });
        Type st = sc.GetType();
        st.InvokeMember("TargetPath", BindingFlags.SetProperty, null, sc, new object[] { target });
        st.InvokeMember("WorkingDirectory", BindingFlags.SetProperty, null, sc, new object[] { workdir });
        // 图标位置必须带引号：目录里带空格（M18 Console）时，shell 解析会失败，快捷方式显示空白图标
        st.InvokeMember("IconLocation", BindingFlags.SetProperty, null, sc, new object[] { "\"" + target + "\",0" });
        st.InvokeMember("Description", BindingFlags.SetProperty, null, sc, new object[] { APP_NAME });
        st.InvokeMember("Save", BindingFlags.InvokeMethod, null, sc, null);
        Marshal.FinalReleaseComObject(sc);
        Marshal.FinalReleaseComObject(shell);
    }

    // 让「开始菜单 / 桌面」立刻刷新，不然新装的条目要等一会儿（甚至要重启）才出现在应用列表里
    [DllImport("shell32.dll", CharSet = CharSet.Unicode)]
    static extern void SHChangeNotify(int eventId, uint flags, IntPtr item1, IntPtr item2);

    static void NotifyShell(string dir)
    {
        try
        {
            SHChangeNotify(0x08000000, 0x0000, IntPtr.Zero, IntPtr.Zero);            // SHCNE_ASSOCCHANGED
            if (!string.IsNullOrEmpty(dir) && Directory.Exists(dir))
            {
                IntPtr p = Marshal.StringToHGlobalUni(dir);
                try { SHChangeNotify(0x00001000, 0x0005, p, IntPtr.Zero); }           // SHCNE_UPDATEDIR | SHCNF_PATHW
                finally { Marshal.FreeHGlobal(p); }
            }
        }
        catch { }
    }

    // ------------------------------------------------------------------ 关掉正在运行的安装版
    static void StopRunning()
    {
        for (int port = 8642; port < 8654; port++)
        {
            try
            {
                string origin = "http://127.0.0.1:" + port;
                var req = (HttpWebRequest)WebRequest.Create(origin + "/api/health");
                req.Timeout = 600;
                req.Proxy = null;
                string body;
                using (var resp = req.GetResponse())
                using (var sr = new StreamReader(resp.GetResponseStream(), Encoding.UTF8)) body = sr.ReadToEnd();
                if (!body.Contains("m18-console")) continue;
                // 源码版不归安装程序管（edition 可能是 \uXXXX 转义）
                var m = Regex.Match(body, "\"edition\"\\s*:\\s*\"([^\"]*)\"");
                if (m.Success && Regex.Unescape(m.Groups[1].Value) == "源码版") continue;
                var q = (HttpWebRequest)WebRequest.Create(origin + "/api/quit");
                q.Method = "POST";
                q.ContentType = "application/json";
                q.Headers.Add("Origin", origin);
                q.Timeout = 3000;
                q.Proxy = null;
                var data = Encoding.ASCII.GetBytes("{}");
                q.ContentLength = data.Length;
                using (var s = q.GetRequestStream()) s.Write(data, 0, data.Length);
                q.GetResponse().Close();
                Thread.Sleep(2500);
            }
            catch { }
        }
    }

    // ------------------------------------------------------------------ 卸载
    static bool Uninstall(bool silent, bool purge)
    {
        if (!silent)
        {
            try { SetProcessDPIAware(); } catch { }
            if (MessageBox.Show("确定要卸载 " + APP_NAME + " 吗？", "卸载", MessageBoxButtons.YesNo, MessageBoxIcon.Question) != DialogResult.Yes)
                return false;
            purge = MessageBox.Show("是否同时删除你的配置、场景、自制插件等数据？\n\n（位于 " + DataDir + "；选「否」会保留，重新安装后还能继续用）",
                                    "卸载", MessageBoxButtons.YesNo, MessageBoxIcon.Question) == DialogResult.Yes;
        }
        string target = InstalledDir();
        if (string.IsNullOrEmpty(target)) target = Path.GetDirectoryName(Assembly.GetExecutingAssembly().Location);
        StopRunning();
        foreach (var lnk in new[] { StartMenuLnk(), DesktopLnk(), StartupLnk() })
        {
            try { if (File.Exists(lnk)) File.Delete(lnk); } catch { }
        }
        try { Registry.CurrentUser.DeleteSubKeyTree(REG_KEY, false); } catch { }
        if (purge)
        {
            try { Directory.Delete(DataDir, true); } catch { }
            try { Directory.Delete(Path.Combine(LocalAppData, "M18Console"), true); } catch { }
        }
        // uninstall.exe 本身在安装目录里、正在运行：交给延迟执行的 cmd 在我们退出后删目录
        if (!string.IsNullOrEmpty(target) && File.Exists(Path.Combine(target, EXE_NAME)))
        {
            var psi = new ProcessStartInfo("cmd.exe", "/c ping 127.0.0.1 -n 4 >nul & rmdir /s /q \"" + target + "\"");
            psi.CreateNoWindow = true;
            psi.UseShellExecute = false;
            psi.WorkingDirectory = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
            Process.Start(psi);
        }
        if (!silent) MessageBox.Show(APP_NAME + " 已卸载。", "卸载", MessageBoxButtons.OK, MessageBoxIcon.Information);
        return true;
    }

    // ------------------------------------------------------------------ 界面
    class InstallForm : Form
    {
        readonly long payloadPos, payloadLen;
        readonly TextBox dirBox = new TextBox();
        readonly CheckBox deskBox = new CheckBox(), runBox = new CheckBox();
        readonly ProgressBar bar = new ProgressBar();
        readonly Label status = new Label();
        readonly Button installBtn = new Button(), browseBtn = new Button();

        public InstallForm(long pos, long len)
        {
            payloadPos = pos; payloadLen = len;
            Text = APP_NAME + " " + VERSION + " 安装";
            Font = new Font("Microsoft YaHei UI", 9f);
            FormBorderStyle = FormBorderStyle.FixedDialog;
            MaximizeBox = false;
            StartPosition = FormStartPosition.CenterScreen;
            AutoScaleMode = AutoScaleMode.Dpi;
            ClientSize = new Size(560, 300);
            try { Icon = Icon.ExtractAssociatedIcon(Assembly.GetExecutingAssembly().Location); } catch { }

            var title = new Label { Text = APP_NAME, Font = new Font("Microsoft YaHei UI", 15f, FontStyle.Bold), AutoSize = true, Location = new Point(20, 16) };
            var sub = new Label { Text = "版本 " + VERSION + " · 自研 Stream Dock M18 驱动控制台", ForeColor = Color.DimGray, AutoSize = true, Location = new Point(22, 52) };
            var dirLabel = new Label { Text = "安装位置：", AutoSize = true, Location = new Point(22, 92) };
            dirBox.SetBounds(100, 88, 350, 26);
            // 已经装过就沿用原来的位置；否则先挑一个「真的能写」的位置预填，
            // 这样双击安装包一路点「安装」就行，不必先撞一次「访问被拒绝」。
            string prefill = InstalledDir();
            if (string.IsNullOrEmpty(prefill)) prefill = FirstWritable(InstallCandidates());
            dirBox.Text = string.IsNullOrEmpty(prefill) ? DefaultDir : prefill;
            browseBtn.Text = "浏览…";
            browseBtn.SetBounds(458, 86, 80, 28);
            browseBtn.Click += delegate
            {
                using (var dlg = new FolderBrowserDialog { Description = "选择安装位置（会在里面建一个 M18 Console 文件夹）" })
                {
                    if (dlg.ShowDialog(this) == DialogResult.OK) dirBox.Text = Path.Combine(dlg.SelectedPath, "M18 Console");
                }
            };
            deskBox.Text = "创建桌面快捷方式"; deskBox.Checked = true; deskBox.AutoSize = true; deskBox.Location = new Point(100, 124);
            runBox.Text = "安装完成后启动"; runBox.Checked = true; runBox.AutoSize = true; runBox.Location = new Point(100, 150);
            var note = new Label
            {
                Text = "需要 Windows 10/11 和 Microsoft Edge（系统自带），不需要管理员权限。\n装了官方 Stream Dock 软件时，还能导入官方场景、图标包和插件。",
                ForeColor = Color.DimGray, AutoSize = true, Location = new Point(22, 184)
            };
            bar.SetBounds(22, 230, 516, 18);
            status.SetBounds(22, 256, 400, 24);
            installBtn.Text = "安装";
            installBtn.SetBounds(438, 254, 100, 32);
            installBtn.Click += delegate { Start(); };
            AcceptButton = installBtn;
            Controls.AddRange(new Control[] { title, sub, dirLabel, dirBox, browseBtn, deskBox, runBox, note, bar, status, installBtn });
        }

        void Start()
        {
            string target = string.IsNullOrWhiteSpace(dirBox.Text) ? DefaultDir : dirBox.Text;
            string why;
            if (!CanWriteUnder(target, out why))
            {
                var report = new List<string>();
                report.Add("× " + target + "  —— " + why);
                var cand = InstallCandidates();
                if (!cand.Contains(target)) cand.Insert(0, target);
                string alt = FirstWritable(cand, report);
                string list = "· " + string.Join("\n· ", report.ToArray());
                if (alt != null)
                {
                    string msg = "这个位置写不进去：\n" + target + "\n\n" + list
                               + "\n\n要改装到下面这个能写的位置吗？\n" + alt
                               + "\n\n（选「否」可以自己点「浏览…」换一个目录）";
                    if (MessageBox.Show(this, msg, "安装位置不可写", MessageBoxButtons.YesNo, MessageBoxIcon.Warning) != DialogResult.Yes) return;
                    dirBox.Text = alt;
                    target = alt;
                }
                else
                {
                    string msg = "这个位置写不进去：\n" + target + "\n\n探测结果：\n" + list
                               + "\n\n多半是安全软件 / Windows 的「受控文件夹访问」在拦未签名的程序。\n"
                               + "运行源码里的「诊断.bat」可以查出是谁拦截并放行。\n\n"
                               + "仍要尝试安装吗？（可能装到一半失败，失败后日志里有确切原因）\n日志：" + LogPath();
                    if (MessageBox.Show(this, msg, "安装位置不可写", MessageBoxButtons.YesNo, MessageBoxIcon.Warning) != DialogResult.Yes) return;
                }
            }
            installBtn.Enabled = browseBtn.Enabled = dirBox.Enabled = false;
            bool desktop = deskBox.Checked, run = runBox.Checked;
            var th = new Thread(() =>
            {
                try
                {
                    string exe = Install(target, desktop, payloadPos, payloadLen, (pct, msg) =>
                        BeginInvoke((Action)(() => { bar.Value = Math.Max(0, Math.Min(100, pct)); status.Text = msg; })));
                    BeginInvoke((Action)(() =>
                    {
                        if (run) Launch(exe);
                        string extra = Warnings.Count == 0 ? "" : "\n\n有 " + Warnings.Count + " 项没做成（不影响使用）：\n· " + string.Join("\n· ", new List<string>(Warnings).ToArray());
                        MessageBox.Show(this, APP_NAME + " 已安装。\n装在：" + exe + extra, "安装完成",
                                        MessageBoxButtons.OK, MessageBoxIcon.Information);
                        Close();
                    }));
                }
                catch (Exception ex)
                {
                    BeginInvoke((Action)(() =>
                    {
                        string detail = "安装失败：" + Explain(ex);
                        // 装在别处被拒 → 主动提议换到默认位置重来，省得用户自己猜
                        bool elsewhere = !string.Equals(Path.GetFullPath(target.Trim()), Path.GetFullPath(DefaultDir), StringComparison.OrdinalIgnoreCase);
                        if (IsAccessDenied(ex) && elsewhere && CanWriteUnder(DefaultDir))
                        {
                            if (MessageBox.Show(this, detail + "\n\n要不要改装到默认位置重试？\n" + DefaultDir, "安装失败",
                                                MessageBoxButtons.YesNo, MessageBoxIcon.Error) == DialogResult.Yes)
                            {
                                dirBox.Text = DefaultDir;
                                installBtn.Enabled = browseBtn.Enabled = dirBox.Enabled = true;
                                status.Text = "";
                                Start();
                                return;
                            }
                        }
                        MessageBox.Show(this, detail, "安装失败", MessageBoxButtons.OK, MessageBoxIcon.Error);
                        installBtn.Enabled = browseBtn.Enabled = dirBox.Enabled = true;
                        status.Text = "";
                    }));
                }
            });
            th.IsBackground = true;
            th.Start();
        }
    }
}
