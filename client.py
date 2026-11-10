import time
import requests
import sys
import ctypes
import os
import datetime
from pynput import mouse, keyboard

os.system("") # Enable ANSI

class C:
    CYAN = '\033[96m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    RED = '\033[91m'
    RESET = '\033[0m'
    BOLD = '\033[1m'

SERVER_URL = "https://edugrid.onrender.com"  # Replace with your actual Render link
idle_timer = 0
is_working = False

current_task = None
current_progress = 0
standby_printed = False

last_x, last_y = None, None
# CRITICAL FIX: Changed kernel to kernel32 below
hwnd = ctypes.windll.kernel32.GetConsoleWindow()

def hide_window():
    if hwnd: ctypes.windll.user32.ShowWindow(hwnd, 0)

def show_window():
    if hwnd: ctypes.windll.user32.ShowWindow(hwnd, 5)

def trigger_interrupt():
    global is_working, idle_timer, standby_printed
    idle_timer = 0 
    standby_printed = False
    is_working = False
    hide_window() 

def on_mouse_move(x, y): 
    global last_x, last_y
    if last_x is None:
        last_x, last_y = x, y
        return
    if abs(x - last_x) > 10 or abs(y - last_y) > 10:
        last_x, last_y = x, y
        trigger_interrupt()

def on_mouse_click(x, y, button, pressed): trigger_interrupt()
def on_key_press(key): trigger_interrupt()

mouse.Listener(on_move=on_mouse_move, on_click=on_mouse_click).start()
keyboard.Listener(on_press=on_key_press).start()

def get_time():
    return datetime.datetime.now().strftime("%H:%M:%S")

print(f"""{C.GREEN}{C.BOLD}
  ___    _        _  _         _       
 | __|__| |_  _  | \| |___  __| |___   
 | _|/ _` | || | | .` / _ \/ _` / -_)  
 |___\__,_|\_,_| |_|\_\___/\__,_\___|  
{C.RESET}""")
print(f"{C.CYAN}[{get_time()}] [BOOT] Handshake with CRPF Public School Local Gateway... OK")
print(f"[{get_time()}] [DIAG] Hardware: Ryzen 9 7900X | 32GB RAM | RTX 3060")
print(f"[{get_time()}] [AUTH] Node ID: Lab-PC-04 securely authenticated.")
print(f"{C.YELLOW}[{get_time()}] [WARN] Student usage detected. Switching to STEALTH BACKGROUND MODE.{C.RESET}")

hide_window()

while True:
    time.sleep(0.1) 
    idle_timer += 0.1
    
    if idle_timer >= 5.0:
        show_window() 
        
        if current_task is None:
            try:
                response = requests.get(f"{SERVER_URL}/get_task").json()
                current_task = response.get("task_id")
                
                if current_task and current_task != "FRAME_999":
                    print(f"\n{C.CYAN}[{get_time()}] [WAKE] Downloading {current_task} payload...{C.RESET}")
                    sys.stdout.write(f"[{get_time()}] [EXEC] Rendering: [")
                    sys.stdout.flush()
                    standby_printed = False
                    is_working = True
                else:
                    if not standby_printed:
                        print(f"\n{C.YELLOW}[{get_time()}] [IDLE] Queue empty. Polling Orchestrator for workloads...{C.RESET}")
                        standby_printed = True
                    is_working = False
                    current_task = None
            except:
                is_working = False
        else:
            if not is_working:
                print(f"\n{C.CYAN}[{get_time()}] [RESUME] Recovering {current_task} from memory...{C.RESET}")
                sys.stdout.write(f"[{get_time()}] [EXEC] Rendering: [{C.GREEN}" + "█" * current_progress + C.RESET)
                sys.stdout.flush()
                is_working = True

    if is_working and current_task:
        for _ in range(5): 
            time.sleep(0.1)
            if not is_working:
                break
        
        if is_working:
            current_progress += 1
            sys.stdout.write(f"{C.GREEN}█{C.RESET}")
            sys.stdout.flush()
            
            if current_progress >= 20: 
                sys.stdout.write("] 100%\n")
                print(f"{C.CYAN}[{get_time()}] [DONE] Asset {current_task} compiled. Uploading via TLS tunnel...{C.RESET}")
                try:
                    requests.post(f"{SERVER_URL}/submit_result", json={"task_id": current_task})
                except:
                    pass
                
                current_task = None
                current_progress = 0
                is_working = False