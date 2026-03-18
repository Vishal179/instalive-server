from flask import Flask, request, jsonify, send_file
import subprocess, os, threading, uuid, time, logging
app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
RECORDINGS_DIR = "/tmp/recordings"
os.makedirs(RECORDINGS_DIR, exist_ok=True)
active_jobs = {}
@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "InstaLive Recorder Server"})
@app.route("/record", methods=["POST"])
def start_recording():
    data = request.json
    if not data: return jsonify({"error": "No data"}), 400
    username = data.get("username")
    cookie = data.get("cookie")
    if not username or not cookie: return jsonify({"error": "username and cookie required"}), 400
    job_id = str(uuid.uuid4())[:8]
    thread = threading.Thread(target=record_live, args=(job_id, username, cookie))
    thread.daemon = True
    thread.start()
    active_jobs[job_id] = {"status": "starting", "username": username, "started_at": time.time(), "file": None, "error": None, "process": None}
    logger.info(f"Started job {job_id} for @{username}")
    return jsonify({"job_id": job_id, "status": "started"})
@app.route("/status/<job_id>", methods=["GET"])
def job_status(job_id):
    if job_id not in active_jobs: return jsonify({"error": "Job not found"}), 404
    job = active_jobs[job_id]
    response = {"job_id": job_id, "status": job["status"], "username": job["username"], "error": job["error"]}
    if job["status"] == "done" and job["file"] and os.path.exists(job["file"]):
        response["file_size"] = os.path.getsize(job["file"])
        response["ready_to_download"] = os.path.getsize(job["file"]) > 0
    return jsonify(response)
@app.route("/download/<job_id>", methods=["GET"])
def download_file(job_id):
    if job_id not in active_jobs: return jsonify({"error": "Job not found"}), 404
    job = active_jobs[job_id]
    if job["status"] != "done": return jsonify({"error": "Not finished", "status": job["status"]}), 202
    if not job["file"] or not os.path.exists(job["file"]): return jsonify({"error": "File not found"}), 404
    return send_file(job["file"], as_attachment=True, download_name=f"{job['username']}_live.mp4", mimetype="video/mp4")
@app.route("/stop/<job_id>", methods=["POST"])
def stop_recording(job_id):
    if job_id not in active_jobs: return jsonify({"error": "Job not found"}), 404
    job = active_jobs[job_id]
    if job.get("process"):
        try: job["process"].terminate(); job["status"] = "stopped"
        except: pass
    return jsonify({"status": "stopped"})
def record_live(job_id, username, cookie):
    output_file = os.path.join(RECORDINGS_DIR, f"{job_id}_{username}.mp4")
    active_jobs[job_id]["status"] = "recording"
    active_jobs[job_id]["file"] = output_file
    try:
        cmd = ["yt-dlp",
            "--add-header", f"Cookie:{cookie}",
            "--add-header", "User-Agent:Mozilla/5.0 (Linux; Android 12) AppleWebKit/537.36 Chrome/105.0 Mobile Safari/537.36",
            "--add-header", "X-IG-App-ID:936619743392459",
            "-f", "best", "-o", output_file,
            "--live-from-start", "--no-part", "--retries", "10",
            f"https://www.instagram.com/{username}/live/"]
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        active_jobs[job_id]["process"] = process
        for line in process.stdout:
            if line.strip(): logger.info(f"[{job_id}] {line.strip()}")
        process.wait()
        if process.returncode == 0 and os.path.exists(output_file) and os.path.getsize(output_file) > 0:
            active_jobs[job_id]["status"] = "done"
        else:
            active_jobs[job_id]["status"] = "failed"
            active_jobs[job_id]["error"] = f"Exit code {process.returncode}"
    except Exception as e:
        active_jobs[job_id]["status"] = "failed"
        active_jobs[job_id]["error"] = str(e)
    threading.Timer(3600, lambda: cleanup_job(job_id)).start()
def cleanup_job(job_id):
    if job_id in active_jobs:
        job = active_jobs[job_id]
        if job.get("file") and os.path.exists(job["file"]):
            try: os.remove(job["file"])
            except: pass
        del active_jobs[job_id]
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
