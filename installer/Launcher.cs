// Jarvis.exe — tiny launcher committed to the repo root.
// First run: opens the installer (installer\install.ps1). Afterwards: starts Jarvis directly - unless an update
// changed the dependencies (pyproject.toml is newer than the last install), then it updates them first.
// Build: powershell -ExecutionPolicy Bypass -File installer\build-launcher.ps1
using System;
using System.Diagnostics;
using System.IO;
using System.Windows.Forms;

[assembly: System.Reflection.AssemblyTitle("J.A.R.V.I.S.")]
[assembly: System.Reflection.AssemblyProduct("jarvis-ai-api")]
[assembly: System.Reflection.AssemblyDescription("Launcher and installer for the Jarvis desktop assistant")]
[assembly: System.Reflection.AssemblyVersion("0.1.0.0")]

static class Launcher
{
    [STAThread]
    static int Main(string[] args)
    {
        string root = AppDomain.CurrentDomain.BaseDirectory.TrimEnd('\\');
        string pythonw = Path.Combine(root, @".venv\Scripts\pythonw.exe");
        string marker = Path.Combine(root, @".venv\.jarvis-installed");
        string installer = Path.Combine(root, @"installer\install.ps1");
        string pyproject = Path.Combine(root, "pyproject.toml");
        bool reinstall = Array.Exists(args, a => a.Equals("--reinstall", StringComparison.OrdinalIgnoreCase));
        bool update = File.Exists(marker) && File.Exists(pyproject)
                      && File.GetLastWriteTimeUtc(pyproject) > File.GetLastWriteTimeUtc(marker);

        try
        {
            if (!reinstall && !update && File.Exists(pythonw) && File.Exists(marker))
            {
                var psi = new ProcessStartInfo(pythonw, "-m jarvis")
                {
                    WorkingDirectory = root,
                    UseShellExecute = false,
                };
                Process.Start(psi);
                return 0;
            }

            if (!File.Exists(installer))
            {
                MessageBox.Show("installer\\install.ps1 is missing.\n\nRe-clone the repository:\n" +
                                "git clone https://github.com/yash2031233/jarvis-ai-api",
                                "Jarvis", MessageBoxButtons.OK, MessageBoxIcon.Error);
                return 1;
            }

            string psArgs = "-NoProfile -ExecutionPolicy Bypass -File \"" + installer + "\"" + (reinstall ? " -Reinstall" : update ? " -Update" : "");
            Process.Start(new ProcessStartInfo("powershell.exe", psArgs)
            {
                WorkingDirectory = root,
                UseShellExecute = true,
            });
            return 0;
        }
        catch (Exception e)
        {
            MessageBox.Show("Couldn't start Jarvis:\n\n" + e.Message, "Jarvis",
                            MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
    }
}
