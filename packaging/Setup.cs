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

    [DllImport("user32.dll")] static extern bool SetProcessDPIAware();

    [STAThread]
    static int Main(string[] argv)
    {
        var args = new List<string>(argv);
        bool silent = args.Contains("--silent");
        try
        {
            long payloadLen;
            long payloadPos = FindPayload(out payloadLen);
            if (args.Contains("--uninstall") || payloadPos < 0)
            {
                return Uninstall(silent, args.Contains("--purge")) ? 0 : 1;
            }
            if (silent)
            {
                int i = args.IndexOf("--dir");
                string target = (i >= 0 && i + 1 < args.Count) ? args[i + 1] : DefaultDir;
                string exe = Install(target, !args.Contains("--no-shortcut"), payloadPos, payloadLen, null);
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
        if (!IsAccessDenied(ex)) return ex.Message;
        string self = Assembly.GetExecutingAssembly().Location;
        string why = ControlledFolderAccess() == 1
            ? "这台电脑开启了 Windows 安全中心的「受控文件夹访问」（勒索软件防护），它会阻止未签名的程序写文件。"
            : "这台电脑的安全软件（或 Windows 安全中心）阻止了安装程序写文件——换任何文件夹都会被拒绝。";
        return ex.Message + "\n\n" + why + "\n\n解决办法（任选其一）：\n"
            + "1. Windows 安全中心 → 病毒和威胁防护 → 勒索软件防护 → 管理勒索软件防护 →\n"
            + "   「通过受控文件夹访问允许应用」，添加本安装程序：\n   " + self + "\n"
            + "   （装好后对安装目录里的 M18Console.exe 也做一次）\n"
            + "2. 在你装的安全软件（360 / 火绒 / 卡巴斯基等）里把安装程序和 M18Console.exe 加入信任\n"
            + "3. 运行源码里的「诊断.bat」：自动查出是谁拦截，并可一键放行\n"
            + "4. 这台电脑上改用源码版（双击「启动 M18 控制台.bat」）";
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
    public static string Install(string target, bool desktop, long payloadPos, long payloadLen, Action<int, string> progress)
    {
        if (progress == null) progress = delegate { };
        target = Path.GetFullPath(target.Trim());
        progress(3, "正在关闭运行中的控制台…");
        StopRunning();
        Directory.CreateDirectory(target);
        string self = Assembly.GetExecutingAssembly().Location;
        string rootFull = target.TrimEnd('\\') + "\\";
        using (var fs = new FileStream(self, FileMode.Open, FileAccess.Read, FileShare.ReadWrite))
        {
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
                    if (entry.FullName.EndsWith("/")) { Directory.CreateDirectory(dest); continue; }
                    Directory.CreateDirectory(Path.GetDirectoryName(dest));
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
            progress(82, "正在写入卸载程序…");
            var head = new byte[payloadPos];
            fs.Seek(0, SeekOrigin.Begin);
            int got = 0;
            while (got < head.Length) { int n = fs.Read(head, got, head.Length - got); if (n <= 0) break; got += n; }
            File.WriteAllBytes(Path.Combine(target, "uninstall.exe"), head);
        }
        string exe = Path.Combine(target, EXE_NAME);
        string uninstaller = Path.Combine(target, "uninstall.exe");
        progress(88, "正在创建快捷方式…");
        MakeShortcut(StartMenuLnk(), exe, target);
        if (desktop) MakeShortcut(DesktopLnk(), exe, target);
        progress(94, "正在登记卸载信息…");
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
        Type t = Type.GetTypeFromProgID("WScript.Shell");
        object shell = Activator.CreateInstance(t);
        object sc = t.InvokeMember("CreateShortcut", BindingFlags.InvokeMethod, null, shell, new object[] { lnk });
        Type st = sc.GetType();
        st.InvokeMember("TargetPath", BindingFlags.SetProperty, null, sc, new object[] { target });
        st.InvokeMember("WorkingDirectory", BindingFlags.SetProperty, null, sc, new object[] { workdir });
        st.InvokeMember("IconLocation", BindingFlags.SetProperty, null, sc, new object[] { target + ",0" });
        st.InvokeMember("Description", BindingFlags.SetProperty, null, sc, new object[] { APP_NAME });
        st.InvokeMember("Save", BindingFlags.InvokeMethod, null, sc, null);
        Marshal.FinalReleaseComObject(sc);
        Marshal.FinalReleaseComObject(shell);
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
            dirBox.Text = InstalledDir() ?? DefaultDir;
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
            installBtn.Enabled = browseBtn.Enabled = dirBox.Enabled = false;
            string target = string.IsNullOrWhiteSpace(dirBox.Text) ? DefaultDir : dirBox.Text;
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
                        MessageBox.Show(this, APP_NAME + " 已安装。\n开始菜单" + (desktop ? "和桌面" : "") + "上有快捷方式。", "安装完成",
                                        MessageBoxButtons.OK, MessageBoxIcon.Information);
                        Close();
                    }));
                }
                catch (Exception ex)
                {
                    BeginInvoke((Action)(() =>
                    {
                        MessageBox.Show(this, "安装失败：" + Explain(ex), "安装失败", MessageBoxButtons.OK, MessageBoxIcon.Error);
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
