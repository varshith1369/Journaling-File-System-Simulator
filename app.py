from flask import Flask, jsonify, request
from flask_cors import CORS
import time
import random
import copy

app = Flask(__name__)
CORS(app)

# ─────────────────────────────────────────────
#  In-memory File System State
# ─────────────────────────────────────────────
fs_state = {
    "disk_blocks": {str(i): None for i in range(1, 17)},  # 16 blocks
    "inode_table": {},        # filename -> {block, size, created, modified}
    "journal": [],            # list of journal entries
    "journal_committed": [],  # committed (checkpointed) entries
    "transaction_id": 0,
    "crash_simulation": False,
    "log": []                 # human-readable event log
}

JOURNAL_STATES = ["BEGIN", "WRITE_DATA", "WRITE_METADATA", "COMMIT", "CHECKPOINT"]

def get_free_block():
    for bid, val in fs_state["disk_blocks"].items():
        if val is None:
            return bid
    return None

def add_log(msg, level="info"):
    fs_state["log"].append({
        "time": time.strftime("%H:%M:%S"),
        "msg": msg,
        "level": level
    })
    if len(fs_state["log"]) > 100:
        fs_state["log"] = fs_state["log"][-100:]

# ─────────────────────────────────────────────
#  Routes
# ─────────────────────────────────────────────

@app.route("/api/state", methods=["GET"])
def get_state():
    return jsonify({
        "disk_blocks": fs_state["disk_blocks"],
        "inode_table": fs_state["inode_table"],
        "journal": fs_state["journal"],
        "journal_committed": fs_state["journal_committed"],
        "transaction_id": fs_state["transaction_id"],
        "log": fs_state["log"][-20:]
    })

@app.route("/api/create_file", methods=["POST"])
def create_file():
    data = request.json
    filename = data.get("filename", "").strip()
    content  = data.get("content", "").strip()
    crash_after = data.get("crash_after", None)  # "begin","data","metadata","commit"

    if not filename:
        return jsonify({"error": "Filename required"}), 400
    if filename in fs_state["inode_table"]:
        return jsonify({"error": f"File '{filename}' already exists"}), 400

    block = get_free_block()
    if block is None:
        return jsonify({"error": "No free blocks available"}), 400

    fs_state["transaction_id"] += 1
    tid = fs_state["transaction_id"]
    steps = []

    # --- STEP 1: BEGIN ---
    journal_entry = {
        "tid": tid,
        "filename": filename,
        "operation": "CREATE",
        "state": "BEGIN",
        "block": block,
        "content": content,
        "timestamp": time.strftime("%H:%M:%S")
    }
    fs_state["journal"].append(copy.deepcopy(journal_entry))
    steps.append({"phase": "BEGIN", "desc": f"[TXN-{tid}] Journal BEGIN: create '{filename}' on block {block}"})
    add_log(f"[TXN-{tid}] Journal BEGIN — create '{filename}'", "journal")

    if crash_after == "begin":
        add_log(f"[TXN-{tid}] 💥 CRASH after BEGIN — file NOT written to disk", "crash")
        return jsonify({"steps": steps, "crashed": True, "recover_hint": "Journal has BEGIN but no COMMIT → on recovery, this transaction is ROLLED BACK (ignored)."})

    # --- STEP 2: WRITE DATA ---
    journal_entry["state"] = "WRITE_DATA"
    fs_state["journal"][-1] = copy.deepcopy(journal_entry)
    steps.append({"phase": "WRITE_DATA", "desc": f"[TXN-{tid}] Writing data block {block} ← '{content}'"})
    add_log(f"[TXN-{tid}] Data written to journal buffer (block {block})", "journal")

    if crash_after == "data":
        add_log(f"[TXN-{tid}] 💥 CRASH after WRITE_DATA — no COMMIT, disk unchanged", "crash")
        return jsonify({"steps": steps, "crashed": True, "recover_hint": "Data was buffered in journal but never committed → ROLLED BACK on recovery."})

    # --- STEP 3: WRITE METADATA ---
    journal_entry["state"] = "WRITE_METADATA"
    fs_state["journal"][-1] = copy.deepcopy(journal_entry)
    steps.append({"phase": "WRITE_METADATA", "desc": f"[TXN-{tid}] Writing inode metadata for '{filename}'"})
    add_log(f"[TXN-{tid}] Metadata written to journal", "journal")

    if crash_after == "metadata":
        add_log(f"[TXN-{tid}] 💥 CRASH after WRITE_METADATA — still no COMMIT", "crash")
        return jsonify({"steps": steps, "crashed": True, "recover_hint": "Metadata in journal but no COMMIT → ROLLED BACK. Disk/inode table untouched."})

    # --- STEP 4: COMMIT ---
    journal_entry["state"] = "COMMIT"
    fs_state["journal"][-1] = copy.deepcopy(journal_entry)
    steps.append({"phase": "COMMIT", "desc": f"[TXN-{tid}] COMMIT written to journal — transaction durable!"})
    add_log(f"[TXN-{tid}] ✅ COMMIT — transaction is now durable", "commit")

    if crash_after == "commit":
        add_log(f"[TXN-{tid}] 💥 CRASH after COMMIT — will REDO on recovery", "crash")
        return jsonify({"steps": steps, "crashed": True, "recover_hint": "COMMIT exists in journal → on recovery, this transaction is REDONE and data is written to disk."})

    # --- STEP 5: APPLY TO DISK (CHECKPOINT) ---
    fs_state["disk_blocks"][block] = {"filename": filename, "content": content}
    fs_state["inode_table"][filename] = {
        "block": block,
        "size": len(content),
        "created": time.strftime("%H:%M:%S"),
        "modified": time.strftime("%H:%M:%S")
    }
    journal_entry["state"] = "CHECKPOINT"
    fs_state["journal"][-1] = copy.deepcopy(journal_entry)
    fs_state["journal_committed"].append(fs_state["journal"].pop())
    steps.append({"phase": "CHECKPOINT", "desc": f"[TXN-{tid}] CHECKPOINT — data flushed to disk, journal entry freed"})
    add_log(f"[TXN-{tid}] 📀 CHECKPOINT — '{filename}' written to disk block {block}", "success")

    return jsonify({"steps": steps, "crashed": False, "filename": filename, "block": block})


@app.route("/api/delete_file", methods=["POST"])
def delete_file():
    data = request.json
    filename = data.get("filename", "").strip()
    crash_after = data.get("crash_after", None)

    if filename not in fs_state["inode_table"]:
        return jsonify({"error": f"File '{filename}' not found"}), 404

    fs_state["transaction_id"] += 1
    tid = fs_state["transaction_id"]
    steps = []
    block = fs_state["inode_table"][filename]["block"]

    journal_entry = {
        "tid": tid, "filename": filename, "operation": "DELETE",
        "state": "BEGIN", "block": block, "timestamp": time.strftime("%H:%M:%S")
    }
    fs_state["journal"].append(copy.deepcopy(journal_entry))
    steps.append({"phase": "BEGIN", "desc": f"[TXN-{tid}] Journal BEGIN: delete '{filename}'"})
    add_log(f"[TXN-{tid}] Journal BEGIN — delete '{filename}'", "journal")

    if crash_after == "begin":
        add_log(f"[TXN-{tid}] 💥 CRASH — file still intact (no COMMIT)", "crash")
        return jsonify({"steps": steps, "crashed": True, "recover_hint": "No COMMIT found → ROLLED BACK. File preserved on disk."})

    journal_entry["state"] = "WRITE_METADATA"
    fs_state["journal"][-1] = copy.deepcopy(journal_entry)
    steps.append({"phase": "WRITE_METADATA", "desc": f"[TXN-{tid}] Mark inode as deleted in journal"})
    add_log(f"[TXN-{tid}] Inode deletion logged", "journal")

    if crash_after == "metadata":
        return jsonify({"steps": steps, "crashed": True, "recover_hint": "No COMMIT → ROLLED BACK. File still safe."})

    journal_entry["state"] = "COMMIT"
    fs_state["journal"][-1] = copy.deepcopy(journal_entry)
    steps.append({"phase": "COMMIT", "desc": f"[TXN-{tid}] COMMIT written"})
    add_log(f"[TXN-{tid}] ✅ COMMIT", "commit")

    if crash_after == "commit":
        return jsonify({"steps": steps, "crashed": True, "recover_hint": "COMMIT found → on recovery, deletion will be REDONE."})

    del fs_state["inode_table"][filename]
    fs_state["disk_blocks"][block] = None
    journal_entry["state"] = "CHECKPOINT"
    fs_state["journal"][-1] = copy.deepcopy(journal_entry)
    fs_state["journal_committed"].append(fs_state["journal"].pop())
    steps.append({"phase": "CHECKPOINT", "desc": f"[TXN-{tid}] CHECKPOINT — block {block} freed"})
    add_log(f"[TXN-{tid}] 📀 CHECKPOINT — '{filename}' deleted, block {block} freed", "success")

    return jsonify({"steps": steps, "crashed": False})


@app.route("/api/recover", methods=["POST"])
def recover():
    """Simulate crash recovery by scanning the journal."""
    recovered = []
    rolled_back = []
    redone = []

    pending = [e for e in fs_state["journal"] if e["state"] != "CHECKPOINT"]
    for entry in pending:
        if entry["state"] == "COMMIT":
            # REDO
            if entry["operation"] == "CREATE":
                fs_state["disk_blocks"][entry["block"]] = {"filename": entry["filename"], "content": entry.get("content","")}
                fs_state["inode_table"][entry["filename"]] = {
                    "block": entry["block"], "size": len(entry.get("content","")),
                    "created": entry["timestamp"], "modified": entry["timestamp"]
                }
                redone.append(f"REDO CREATE '{entry['filename']}' (TXN-{entry['tid']})")
                add_log(f"🔄 REDO TXN-{entry['tid']}: CREATE '{entry['filename']}'", "recover")
            elif entry["operation"] == "DELETE":
                fname = entry["filename"]
                if fname in fs_state["inode_table"]:
                    del fs_state["inode_table"][fname]
                fs_state["disk_blocks"][entry["block"]] = None
                redone.append(f"REDO DELETE '{entry['filename']}' (TXN-{entry['tid']})")
                add_log(f"🔄 REDO TXN-{entry['tid']}: DELETE '{entry['filename']}'", "recover")
            entry["state"] = "CHECKPOINT"
            fs_state["journal_committed"].append(entry)
            recovered.append(entry["tid"])
        else:
            # ROLLBACK
            rolled_back.append(f"ROLLBACK TXN-{entry['tid']} ({entry['operation']} '{entry['filename']}')")
            add_log(f"↩️ ROLLBACK TXN-{entry['tid']}: {entry['operation']} '{entry['filename']}'", "recover")

    fs_state["journal"] = [e for e in fs_state["journal"] if e["tid"] not in recovered and e["state"] != "CHECKPOINT"]

    return jsonify({
        "redone": redone,
        "rolled_back": rolled_back,
        "message": f"Recovery complete. {len(redone)} redone, {len(rolled_back)} rolled back."
    })


@app.route("/api/reset", methods=["POST"])
def reset():
    fs_state["disk_blocks"] = {str(i): None for i in range(1, 17)}
    fs_state["inode_table"] = {}
    fs_state["journal"] = []
    fs_state["journal_committed"] = []
    fs_state["transaction_id"] = 0
    fs_state["log"] = []
    add_log("🔃 File system reset", "info")
    return jsonify({"message": "File system reset."})


@app.route("/api/files", methods=["GET"])
def list_files():
    return jsonify({"files": list(fs_state["inode_table"].keys()), "inode_table": fs_state["inode_table"]})


if __name__ == "__main__":
    add_log("📂 Journaling File System Simulator started", "info")
    app.run(debug=True, port=5000)
