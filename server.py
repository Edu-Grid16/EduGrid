import os
import datetime
from flask import Flask, jsonify, request, render_template
import logging

# Magic command to enable ANSI Colors in Windows CMD
os.system("") 

class C:
    CYAN = '\033[96m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    RED = '\033[91m'
    MAGENTA = '\033[95m'
    RESET = '\033[0m'
    BOLD = '\033[1m'

log = logging.getLogger('werkzeug')
log.disabled = True

app = Flask(__name__)

tasks = [] 
total_revenue = 0
task_counter = 0
total_deployed = 0
total_completed = 0

# === ULTIMATE CORS FIX ===
# This tells mobile and desktop browsers to allow connections across your Wi-Fi
@app.after_request
def after_request(response):
    response.headers.add('Access-Control-Allow-Origin', '*')
    response.headers.add('Access-Control-Allow-Headers', 'Content-Type,Authorization')
    response.headers.add('Access-Control-Allow-Methods', 'GET,PUT,POST,DELETE,OPTIONS')
    return response

def log_sys(msg):
    t = datetime.datetime.now().strftime("%H:%M:%S")
    print(f"{C.CYAN}[{t}] [SYS-CORE]{C.RESET} {msg}")

def log_cloud(msg):
    t = datetime.datetime.now().strftime("%H:%M:%S")
    print(f"{C.MAGENTA}[{t}] [EDGE-ROUTER]{C.RESET} {msg}")

def log_success(msg):
    t = datetime.datetime.now().strftime("%H:%M:%S")
    print(f"{C.GREEN}[{t}] [FINANCE]{C.RESET} {msg}")

# Stunning ASCII Banner
print(f"""{C.CYAN}{C.BOLD}
    ______    __      ______     _     __ 
   / ____/___/ /_  __/ ____/____(_)___/ / 
  / __/ / __  / / / / / __/ ___/ / __  /  
 / /___/ /_/ / /_/ / /_/ / /  / / /_/ /   
/_____/\__,_/\__,_/\____/_/  /_/\__,_/    
                                          
=== ENTERPRISE ORCHESTRATOR v2.1.0 (PROD) ==={C.RESET}
""")

log_sys("Initializing EduGrid Central Command...")
log_sys("Binding to port 5000: HTTP REST API Online")
log_sys("Monitoring network for idle CRPF School infrastructure...\n")

@app.route('/school')
def school_dashboard(): return render_template('school.html')

@app.route('/startup')
def startup_dashboard(): return render_template('startup.html')

@app.route('/api/data')
def get_live_data(): return jsonify({"revenue": total_revenue})

@app.route('/api/startup_status')
def startup_status():
    return jsonify({"deployed": total_deployed, "completed": total_completed})

# Added 'OPTIONS' method to catch the browser preflight security checks
@app.route('/deploy_workload', methods=['POST', 'OPTIONS'])
def deploy_workload():
    if request.method == 'OPTIONS':
        return jsonify({"status": "ok"}), 200
        
    global task_counter, total_deployed
    data = request.json
    num_frames = int(data.get('frames', 0))
    total_deployed += num_frames
    
    for _ in range(num_frames):
        task_counter += 1
        tasks.append({"task_id": f"FRAME_{str(task_counter).zfill(3)}"})
        
    print("") # Blank line for spacing
    log_cloud(f"Received secure AES-256 payload: {num_frames} frames from Startup Console.")
    log_cloud(f"Queue Size Expanded: {len(tasks)} tasks pending dispatch.")
    return jsonify({"status": "success", "message": f"{num_frames} frames queued"})

@app.route('/get_task', methods=['GET'])
def get_task():
    if len(tasks) > 0:
        return jsonify(tasks.pop(0))
    return jsonify({"task_id": None}) 

@app.route('/submit_result', methods=['POST', 'OPTIONS'])
def submit_result():
    if request.method == 'OPTIONS':
        return jsonify({"status": "ok"}), 200
        
    global total_revenue, total_completed
    data = request.json
    revenue_earned = 250 
    total_revenue += revenue_earned
    total_completed += 1
    
    log_success(f"Verified {C.BOLD}{data['task_id']}{C.RESET}{C.GREEN} rendered by CRPF Lab PC.")
    log_success(f"CRPF Wallet Credited: {C.BOLD}+₹{revenue_earned}{C.RESET}{C.GREEN} (Net Yield: ₹{total_revenue})")
    return jsonify({"status": "success"})

if __name__ == '__main__':
    # Binding to 0.0.0.0 enables the phone connection across Wi-Fi
    app.run(host='0.0.0.0', port=5000)