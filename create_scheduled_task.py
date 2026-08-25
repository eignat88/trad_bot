"""Create Windows scheduled task for scanner auto-start."""
import os
import win32com.client
import pythoncom

pythoncom.CoInitialize()
scheduler = win32com.client.Dispatch("Schedule.Service")
scheduler.Connect()

folder = scheduler.GetFolder("\\")
username = os.getlogin()

# Create task definition via the service
task_def = scheduler.NewTask(0)

# Trigger: at system startup
trigger = task_def.Triggers.Create(8)  # TASK_TRIGGER_BOOT
trigger.Enabled = True

# Action: run python
action = task_def.Actions.Create(0)  # TASK_ACTION_EXEC
action.Path = r"D:\Python\python.exe"
action.Arguments = r"D:\py_pro\trad_bot\scanner_runner.py"
action.WorkingDirectory = r"D:\py_pro\trad_bot"

# Settings
task_def.Settings.Enabled = True
task_def.Settings.StartWhenAvailable = True
task_def.Settings.DisallowStartIfOnBatteries = False
task_def.Settings.StopIfGoingOnBatteries = False

# Principal: run as current user with highest privileges
task_def.Principal.UserId = username
task_def.Principal.LogonType = 3  # TASK_LOGON_INTERACTIVE_TOKEN
task_def.Principal.RunLevel = 1  # TASK_RUNLEVEL_HIGHEST

# Register (6 = TASK_CREATE_OR_UPDATE)
folder.RegisterTaskDefinition("BybitScanner", task_def, 6, username, None, 3)
print("Task BybitScanner created successfully!")
print(f"  Trigger: on system startup")
print(f"  Command: D:\\Python\\python.exe D:\\py_pro\\trad_bot\\scanner_runner.py")
print(f"  Run as: {username} (interactive token, highest privileges)")
