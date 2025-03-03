#!/usr/bin/env python3
"""
VSCode Memory Manager per macOS

Questo script identifica le finestre di VS Code inattive e usa alcune tecniche 
per ridurre il loro consumo di memoria.
"""

import subprocess
import time
import os
import sys
import argparse
import json
from datetime import datetime, timedelta

def get_vscode_processes():
    """Ottiene tutti i processi di VS Code attualmente in esecuzione."""
    try:
        # Ottiene i processi VS Code con informazioni sulla memoria e sul tempo di esecuzione
        result = subprocess.run(
            ["ps", "-eo", "pid,rss,etime,command"],
            capture_output=True,
            text=True,
            check=True
        )
        
        processes = []
        for line in result.stdout.splitlines():
            if "Visual Studio Code.app/Contents/MacOS/Electron" in line and "Helper" not in line:
                parts = line.split()
                pid = int(parts[0])
                memory_kb = int(parts[1])
                elapsed_time = parts[2]
                
                # Converti elapsed_time in minuti
                if "-" in elapsed_time:  # giorni-ore:min:sec
                    days, rest = elapsed_time.split("-")
                    hours, minutes, seconds = map(int, rest.split(":"))
                    total_minutes = int(days) * 24 * 60 + hours * 60 + minutes
                elif elapsed_time.count(":") == 2:  # ore:min:sec
                    hours, minutes, seconds = map(int, elapsed_time.split(":"))
                    total_minutes = hours * 60 + minutes
                else:  # min:sec
                    minutes, seconds = map(int, elapsed_time.split(":"))
                    total_minutes = minutes
                
                processes.append({
                    "pid": pid,
                    "memory_kb": memory_kb,
                    "elapsed_minutes": total_minutes,
                    "command": " ".join(parts[3:])
                })
        
        return processes
    except subprocess.SubprocessError as e:
        print(f"Errore nell'ottenere i processi: {e}")
        return []

def get_window_focus_info():
    """Ottiene informazioni su quali finestre sono attualmente in focus."""
    try:
        # Questo comando AppleScript ottiene l'ID delle finestre in focus
        script = """
        tell application "System Events"
            set frontApp to name of first application process whose frontmost is true
            return frontApp
        end tell
        """
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            check=True
        )
        return result.stdout.strip()
    except subprocess.SubprocessError as e:
        print(f"Errore nell'ottenere le informazioni sul focus: {e}")
        return None

def is_vscode_window_active():
    """Verifica se una finestra di VS Code è attualmente attiva."""
    active_app = get_window_focus_info()
    return active_app == "Code" or active_app == "Visual Studio Code"

def suspend_process(pid):
    """Sospende temporaneamente un processo."""
    try:
        subprocess.run(["kill", "-STOP", str(pid)], check=True)
        return True
    except subprocess.SubprocessError as e:
        print(f"Errore nella sospensione del processo {pid}: {e}")
        return False

def resume_process(pid):
    """Riprende un processo sospeso."""
    try:
        subprocess.run(["kill", "-CONT", str(pid)], check=True)
        return True
    except subprocess.SubprocessError as e:
        print(f"Errore nella ripresa del processo {pid}: {e}")
        return False

def reduce_process_priority(pid):
    """Riduce la priorità di un processo usando nice."""
    try:
        # Aumenta il valore nice (riduce la priorità)
        subprocess.run(["renice", "10", "-p", str(pid)], check=True)
        return True
    except subprocess.SubprocessError as e:
        print(f"Errore nella riduzione della priorità del processo {pid}: {e}")
        return False

def compress_memory():
    """Comprime la memoria inutilizzata a livello di sistema."""
    try:
        subprocess.run(["sudo", "purge"], check=True)
        return True
    except subprocess.SubprocessError as e:
        print(f"Errore nella compressione della memoria: {e}")
        return False

def get_process_window_titles(pid):
    """Ottiene i titoli delle finestre associate a un processo VS Code."""
    try:
        script = f"""
        tell application "System Events"
            set windowTitles to {{}}
            tell process "Code"
                set windowList to every window
                repeat with aWindow in windowList
                    copy (title of aWindow) to the end of windowTitles
                end repeat
            end tell
            return windowTitles
        end tell
        """
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True
        )
        return result.stdout.strip().split(", ")
    except subprocess.SubprocessError:
        return ["Unknown"]

def save_state(hibernated_processes, state_file):
    """Salva lo stato dei processi ibernati in un file."""
    with open(state_file, 'w') as f:
        json.dump(hibernated_processes, f)

def load_state(state_file):
    """Carica lo stato dei processi ibernati da un file."""
    if os.path.exists(state_file):
        with open(state_file, 'r') as f:
            return json.load(f)
    return {}

def hibernate_inactive_vscode_windows(threshold_minutes=30, memory_threshold_mb=500, state_file=None):
    """Iberna le finestre VS Code inattive basandosi sul tempo e sull'uso della memoria."""
    if state_file is None:
        state_file = os.path.expanduser("~/.vscode_hibernated_processes.json")
    
    hibernated_processes = load_state(state_file)
    vscode_processes = get_vscode_processes()
    
    is_active = is_vscode_window_active()
    now = datetime.now()
    
    if is_active:
        # Se VS Code è attivo, risveglia tutti i processi ibernati
        print("VS Code è attivo, risveglio tutti i processi ibernati...")
        for pid_str, info in list(hibernated_processes.items()):
            pid = int(pid_str)
            if resume_process(pid):
                print(f"Processo {pid} ({info.get('title', 'Unknown')}) risvegliato.")
                del hibernated_processes[pid_str]
    else:
        # Esamina i processi VS Code per l'ibernazione
        for process in vscode_processes:
            pid = process["pid"]
            memory_mb = process["memory_kb"] / 1024
            elapsed_minutes = process["elapsed_minutes"]
            
            # Controlla se il processo è già ibernato
            if str(pid) in hibernated_processes:
                continue
            
            # Controlla se il processo supera le soglie per l'ibernazione
            if elapsed_minutes > threshold_minutes and memory_mb > memory_threshold_mb:
                window_titles = get_process_window_titles(pid)
                title_info = " | ".join(window_titles) if window_titles else "Unknown"
                
                print(f"Ibernazione processo VS Code {pid} (memoria: {memory_mb:.2f} MB, "
                      f"inattivo da: {elapsed_minutes} minuti, finestre: {title_info})")
                
                if suspend_process(pid) and reduce_process_priority(pid):
                    hibernated_processes[str(pid)] = {
                        "hibernated_at": now.isoformat(),
                        "memory_mb": memory_mb,
                        "title": title_info
                    }
                    print(f"Processo {pid} ibernato con successo.")
    
    # Compressione della memoria a livello di sistema
    if not is_active and vscode_processes:
        print("Compressione della memoria di sistema...")
        compress_memory()
    
    # Salva lo stato aggiornato
    save_state(hibernated_processes, state_file)
    return hibernated_processes

def main():
    parser = argparse.ArgumentParser(description="Gestione memoria VS Code per macOS")
    parser.add_argument("--threshold", type=int, default=30,
                        help="Soglia di inattività in minuti (default: 30)")
    parser.add_argument("--memory", type=int, default=500,
                        help="Soglia di utilizzo memoria in MB (default: 500)")
    parser.add_argument("--interval", type=int, default=5,
                        help="Intervallo di controllo in minuti (default: 5)")
    parser.add_argument("--daemon", action="store_true",
                        help="Esegui come daemon in background")
    parser.add_argument("--state-file", type=str,
                        default=os.path.expanduser("~/.vscode_hibernated_processes.json"),
                        help="File per salvare lo stato dell'ibernazione")
    
    args = parser.parse_args()
    
    print(f"VS Code Memory Manager avviato con:")
    print(f"- Soglia inattività: {args.threshold} minuti")
    print(f"- Soglia memoria: {args.memory} MB")
    print(f"- File di stato: {args.state_file}")
    
    if args.daemon:
        print(f"Esecuzione in modalità daemon con intervallo di {args.interval} minuti")
        while True:
            hibernated = hibernate_inactive_vscode_windows(
                args.threshold, args.memory, args.state_file
            )
            time.sleep(args.interval * 60)
    else:
        hibernated = hibernate_inactive_vscode_windows(
            args.threshold, args.memory, args.state_file
        )
        if hibernated:
            print(f"Processi ibernati: {len(hibernated)}")
            for pid, info in hibernated.items():
                hibernated_at = datetime.fromisoformat(info["hibernated_at"])
                age = (datetime.now() - hibernated_at).total_seconds() / 60
                print(f"- PID {pid}: {info['title']}, {info['memory_mb']:.2f} MB, "
                      f"ibernato da {age:.1f} minuti")
        else:
            print("Nessun processo ibernato.")

if __name__ == "__main__":
    main()