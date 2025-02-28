#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MacOS Memory Priority Manager - Versione avanzata con sicurezza

Questo script permette di dare priorità a un processo specifico mettendo in pausa
i processi non essenziali che consumano molta RAM, in modo da facilitare l'uso dello swap
e ridurre la pressione sulla memoria, inclusa la memoria video condivisa (MPS).

Funzionalità:
- Modalità di attesa iniziale (attende "start" per avviarsi)
- Possibilità di gestire specifici processi senza avviare il sistema completo
- Sistema di sicurezza per garantire il ripristino dei processi in caso di crash
- Evita di bloccare Terminal.app, processi bash e il processo padre
- Supporta input interattivo per aggiungere/rimuovere processi dalla lista di esclusione
- Integra funzionalità macOS specifiche per la gestione della memoria
"""

import os
import sys
import signal
import time
import subprocess
import argparse
import re
import threading
import tempfile
import json
import atexit
import psutil
from datetime import datetime
import select

# Verifica se psutil è installato
try:
    import psutil
except ImportError:
    print("\nErrore: il modulo 'psutil' non è installato.")
    print("Installa psutil con: pip install psutil")
    sys.exit(1)

# Lista di processi essenziali che non dovrebbero mai essere sospesi
ESSENTIAL_PROCESSES = [
    'launchd', 'kernel_task', 'WindowServer', 'SystemUIServer', 'Finder',
    'Dock', 'loginwindow', 'mds', 'mds_stores', 'opendirectoryd', 'securityd',
    'coreaudiod', 'syslogd', 'configd', 'distnoted', 'notifyd', 'cfprefsd',
    'secd', 'networkd', 'apsd', 'amfid', 'syspolicyd', 'cloudkitd', 'powerd',
    'coreduetd', 'airportd', 'bluetoothd', 'locationd', 'diagnosticd', 'usbd',
    'Activity Monitor'
]

# Processi shell che non dovrebbero mai essere sospesi
SHELL_PROCESSES = [
    'Terminal', 'iTerm2', 'iTerm', 'bash', 'zsh', 'sh', 'fish',
    'python', 'Python', 'python3', 'ssh', 'tmux', 'screen'
]

# Processi IDE / editor
IDE_PROCESSES = [
    'PyCharm', 'pycharm', 'VSCode', 'code', 'Xcode', 'Atom', 'Sublime Text',
    'Eclipse', 'IntelliJ IDEA', 'Vim', 'vim', 'nvim', 'MacVim', 'emacs'
]

# Configurazione del file di stato per il meccanismo di sicurezza
STATE_DIR = os.path.join(tempfile.gettempdir(), 'macos_ram_manager')
STATE_FILE = os.path.join(STATE_DIR, 'paused_processes.json')

# Assicurati che la directory esista
if not os.path.exists(STATE_DIR):
    os.makedirs(STATE_DIR, exist_ok=True)

class ProcessInfo:
    def __init__(self, pid, name, memory_mb=0, cpu_percent=0):
        self.pid = pid
        self.name = name
        self.memory_mb = memory_mb
        self.cpu_percent = cpu_percent
        self.paused = False
        self.parent_pid = None
        try:
            self.parent_pid = psutil.Process(pid).ppid()
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            pass
        
    def __str__(self):
        status = "PAUSED" if self.paused else "RUNNING"
        return f"PID: {self.pid}, Name: {self.name}, Memory: {self.memory_mb:.2f} MB, CPU: {self.cpu_percent:.1f}%, Status: {status}"
    
    def to_dict(self):
        """Converte l'oggetto in un dizionario per la serializzazione JSON."""
        return {
            'pid': self.pid,
            'name': self.name,
            'memory_mb': self.memory_mb,
            'cpu_percent': self.cpu_percent,
            'paused': self.paused,
            'parent_pid': self.parent_pid
        }
    
    @classmethod
    def from_dict(cls, data):
        """Crea un oggetto ProcessInfo da un dizionario."""
        proc = cls(data['pid'], data['name'], data['memory_mb'], data['cpu_percent'])
        proc.paused = data['paused']
        proc.parent_pid = data['parent_pid']
        return proc

def save_paused_processes(paused_processes):
    """Salva lo stato dei processi in pausa nel file di stato."""
    try:
        data = [proc.to_dict() for proc in paused_processes]
        with open(STATE_FILE, 'w') as f:
            json.dump(data, f)
    except Exception as e:
        print(f"Errore nel salvataggio dello stato: {e}")

def load_paused_processes():
    """Carica lo stato dei processi in pausa dal file di stato."""
    if not os.path.exists(STATE_FILE):
        return []
    
    try:
        with open(STATE_FILE, 'r') as f:
            data = json.load(f)
        return [ProcessInfo.from_dict(item) for item in data]
    except Exception as e:
        print(f"Errore nel caricamento dello stato: {e}")
        return []

def cleanup_state_file():
    """Rimuove il file di stato."""
    if os.path.exists(STATE_FILE):
        try:
            os.remove(STATE_FILE)
        except Exception as e:
            print(f"Errore nella rimozione del file di stato: {e}")

def run_command(command):
    """Esegue un comando shell e restituisce l'output."""
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True, text=True)
    stdout, stderr = process.communicate()
    if process.returncode != 0:
        print(f"Errore nell'esecuzione del comando: {command}")
        print(f"Stderr: {stderr}")
        return None
    return stdout

def get_process_by_name_or_pid(identifier):
    """Trova un processo dato il nome o il PID."""
    try:
        # Verifica se l'identificatore è un PID (numero)
        pid = int(identifier)
        # Verifica se il processo esiste
        try:
            process = psutil.Process(pid)
            return pid
        except psutil.NoSuchProcess:
            return None
    except ValueError:
        # L'identificatore è un nome di processo
        for proc in psutil.process_iter(['pid', 'name']):
            try:
                if identifier.lower() in proc.info['name'].lower():
                    return proc.info['pid']
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    
    return None

def get_parent_process_chain(pid):
    """Ottiene la catena di processi padri a partire da un PID."""
    parent_chain = []
    try:
        current_pid = pid
        while current_pid and current_pid != 1:  # 1 è il PID di launchd (init)
            proc = psutil.Process(current_pid)
            parent_chain.append((current_pid, proc.name()))
            current_pid = proc.ppid()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass
    
    return parent_chain

def get_system_memory_info():
    """Ottiene informazioni sulla memoria di sistema."""
    # Utilizziamo psutil per informazioni più dettagliate
    vm = psutil.virtual_memory()
    swap = psutil.swap_memory()
    
    # Ottieniamo anche le statistiche di vm_stat per informazioni più specifiche di macOS
    command = "vm_stat"
    output = run_command(command)
    vm_stat_info = {}
    
    if output:
        # Estrai la dimensione della pagina (in genere 4096 bytes su macOS)
        page_size = 4096  # Default page size
        page_size_match = re.search(r'page size of (\d+) bytes', output)
        if page_size_match:
            page_size = int(page_size_match.group(1))
        
        for line in output.strip().split('\n'):
            if ":" in line:
                key, value = line.split(':', 1)
                key = key.strip()
                value = int(value.strip().replace('.', ''))
                vm_stat_info[key] = value * page_size / (1024 * 1024)  # Converti in MB
    
    # Ottieni informazioni sulla pressione di memoria (disponibile su macOS recenti)
    memory_pressure = None
    command = "memory_pressure"
    try:
        output = run_command(command)
        if output:
            match = re.search(r'System-wide memory pressure: (\d+)%', output)
            if match:
                memory_pressure = int(match.group(1))
    except:
        pass
    
    return {
        'total': vm.total / (1024 * 1024),  # MB
        'available': vm.available / (1024 * 1024),  # MB
        'used': vm.used / (1024 * 1024),  # MB
        'free': vm.free / (1024 * 1024),  # MB
        'percent': vm.percent,
        'swap_total': swap.total / (1024 * 1024),  # MB
        'swap_used': swap.used / (1024 * 1024),  # MB
        'swap_free': swap.free / (1024 * 1024),  # MB
        'swap_percent': swap.percent,
        'vm_stat': vm_stat_info,
        'memory_pressure': memory_pressure
    }

def get_processes_memory_usage(priority_pid, parent_chain_pids, user_excluded_pids):
    """Ottiene l'utilizzo di memoria e CPU di tutti i processi."""
    processes = []
    
    for proc in psutil.process_iter(['pid', 'name', 'memory_info', 'cpu_percent']):
        try:
            if proc.info['pid'] == priority_pid:
                continue
                
            # Calcola l'utilizzo della memoria in MB
            memory_mb = proc.info['memory_info'].rss / (1024 * 1024)
            
            process_info = ProcessInfo(
                proc.info['pid'],
                proc.info['name'],
                memory_mb,
                proc.info['cpu_percent']
            )
            
            processes.append(process_info)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    
    # Ordina i processi per utilizzo di memoria (in ordine decrescente)
    processes.sort(key=lambda p: p.memory_mb, reverse=True)
    return processes

def is_essential(process, parent_chain_pids, user_excluded_pids, current_app_name=None):
    """
    Verifica se un processo è essenziale o dovrebbe essere escluso dalla sospensione.
    """
    # Verifica se il processo è nella lista di esclusione dell'utente
    if process.pid in user_excluded_pids:
        return True
    
    # Verifica se il processo è nella catena dei genitori
    if process.pid in parent_chain_pids:
        return True
    
    # Verifica se è un processo shell
    for shell in SHELL_PROCESSES:
        if process.name.lower() == shell.lower() or shell.lower() in process.name.lower():
            return True
    
    # Verifica se è un processo IDE e se fa parte dell'applicazione corrente
    if current_app_name:
        for ide in IDE_PROCESSES:
            if ide.lower() in current_app_name.lower() and ide.lower() in process.name.lower():
                return True
    
    # Verifica se è un processo essenziale di sistema
    for essential in ESSENTIAL_PROCESSES:
        if process.name.lower() == essential.lower() or essential.lower() in process.name.lower():
            return True
    
    # Verifica se è un processo figlio di un terminale o shell
    try:
        parent = psutil.Process(process.parent_pid) if process.parent_pid else None
        if parent:
            parent_name = parent.name()
            for shell in SHELL_PROCESSES:
                if shell.lower() in parent_name.lower():
                    return True
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass
    
    return False

def pause_process(process):
    """Mette in pausa un processo."""
    try:
        os.kill(process.pid, signal.SIGSTOP)
        process.paused = True
        # Salva lo stato corrente
        return True
    except Exception as e:
        print(f"Impossibile mettere in pausa il processo {process.pid}: {e}")
        return False

def resume_process(process):
    """Riprende un processo precedentemente messo in pausa."""
    try:
        os.kill(process.pid, signal.SIGCONT)
        process.paused = False
        return True
    except Exception as e:
        print(f"Impossibile riprendere il processo {process.pid}: {e}")
        return False

def force_process_swap(pid):
    """
    Tenta di forzare lo swap di un processo utilizzando funzionalità specifiche di macOS.
    """
    try:
        # Verifica se l'utility purge è disponibile (richiede privilegi di amministratore)
        if os.geteuid() == 0:  # Verifica se siamo root
            # Purge forza il sistema a liberare la memoria cache e buffer
            purge_output = run_command("purge")
            
            # Utilizzo di memory_pressure per forzare la pressione di memoria
            pageout_cmd = f"sudo memory_pressure -l warn -p {pid}"
            pageout_output = run_command(pageout_cmd)
            
            if pageout_output:
                print(f"Forzato lo swap per il processo {pid}")
                return True
    except Exception as e:
        print(f"Errore durante il tentativo di forzare lo swap: {e}")
    
    return False

def handle_single_process(pid, action, user_excluded_pids):
    """
    Gestisce un singolo processo (pausa/ripresa) senza avviare l'intero sistema.
    
    Args:
        pid: PID del processo da gestire
        action: "pause" o "resume"
        user_excluded_pids: Set di PID esclusi dall'utente
    """
    try:
        proc = psutil.Process(pid)
        process_name = proc.name()
        
        # Verifica se il processo è essenziale
        process_info = ProcessInfo(pid, process_name)
        if is_essential(process_info, [os.getpid()], user_excluded_pids):
            print(f"Il processo {pid} ({process_name}) è considerato essenziale o protetto.")
            print("Vuoi forzare l'operazione? (s/n): ", end='')
            choice = input().strip().lower()
            if choice != 's' and choice != 'y' and choice != 'yes' and choice != 'si':
                print("Operazione annullata.")
                return
        
        # Esegui l'azione richiesta
        if action == "pause":
            memory_mb = proc.memory_info().rss / (1024 * 1024)
            process_info = ProcessInfo(pid, process_name, memory_mb, proc.cpu_percent())
            print(f"Mettendo in pausa il processo: {process_info}")
            
            if pause_process(process_info):
                print(f"Processo {pid} ({process_name}) messo in pausa con successo")
                
                # Salva lo stato per il ripristino di sicurezza
                paused_processes = load_paused_processes()
                paused_processes.append(process_info)
                save_paused_processes(paused_processes)
                
                print("Processo salvato nel file di stato per il ripristino di sicurezza.")
        
        elif action == "resume":
            process_info = ProcessInfo(pid, process_name)
            process_info.paused = True  # Assumiamo che sia in pausa
            
            print(f"Riprendendo il processo: {process_info}")
            if resume_process(process_info):
                print(f"Processo {pid} ({process_name}) ripreso con successo")
                
                # Aggiorna il file di stato
                paused_processes = load_paused_processes()
                paused_processes = [p for p in paused_processes if p.pid != pid]
                save_paused_processes(paused_processes)
    
    except psutil.NoSuchProcess:
        print(f"Il processo con PID {pid} non esiste.")
    except Exception as e:
        print(f"Errore nella gestione del processo {pid}: {e}")

def input_listener(user_excluded_pids, paused_processes, stop_event, started_event):
    """
    Thread per ascoltare gli input dell'utente per aggiungere/rimuovere processi 
    dalla lista di esclusione o avviare il monitoraggio.
    """
    print("\nComandi disponibili:")
    if not started_event.is_set():
        print("  start                  : Avvia il monitoraggio della memoria")
    print("  +nome_processo o +pid : Aggiungi un processo alla lista di esclusione")
    print("  -nome_processo o -pid : Rimuovi un processo dalla lista di esclusione")
    print("  --processo_pid        : Metti in pausa un processo specifico")
    print("  ++processo_pid        : Riprendi un processo specifico")
    print("  list                  : Mostra la lista dei processi esclusi")
    print("  help                  : Mostra questo aiuto")
    print("  quit o exit           : Termina il programma\n")
    
    while not stop_event.is_set():
        # Utilizziamo select per verificare se c'è input disponibile
        # senza bloccare il thread
        rlist, _, _ = select.select([sys.stdin], [], [], 0.5)
        
        if rlist:
            cmd = sys.stdin.readline().strip()
            
            # Comando per avviare il monitoraggio
            if cmd.lower() == 'start' and not started_event.is_set():
                started_event.set()
                print("Monitoraggio avviato!")
                continue
            
            elif cmd.lower() in ['quit', 'exit', 'q']:
                stop_event.set()
                print("Terminazione richiesta...")
                break
            
            elif cmd.lower() == 'help':
                print("\nComandi disponibili:")
                if not started_event.is_set():
                    print("  start                  : Avvia il monitoraggio della memoria")
                print("  +nome_processo o +pid : Aggiungi un processo alla lista di esclusione")
                print("  -nome_processo o -pid : Rimuovi un processo dalla lista di esclusione")
                print("  --processo_pid        : Metti in pausa un processo specifico")
                print("  ++processo_pid        : Riprendi un processo specifico")
                print("  list                  : Mostra la lista dei processi esclusi")
                print("  paused                : Mostra la lista dei processi in pausa")
                print("  help                  : Mostra questo aiuto")
                print("  quit o exit           : Termina il programma\n")
            
            elif cmd.lower() == 'list':
                if not user_excluded_pids:
                    print("Nessun processo nella lista di esclusione.")
                else:
                    print("\nProcessi nella lista di esclusione:")
                    for pid in user_excluded_pids:
                        try:
                            proc = psutil.Process(pid)
                            print(f"  PID: {pid}, Nome: {proc.name()}")
                        except:
                            print(f"  PID: {pid}, Nome: sconosciuto (processo terminato)")
            
            elif cmd.lower() == 'paused':
                current_paused = [p for p in paused_processes if p.paused]
                if not current_paused:
                    print("Nessun processo attualmente in pausa.")
                else:
                    print("\nProcessi attualmente in pausa:")
                    for proc in current_paused:
                        print(f"  {proc}")
            
            elif cmd.startswith('+') and not cmd.startswith('++'):
                target = cmd[1:].strip()
                if target:
                    pid = get_process_by_name_or_pid(target)
                    if pid:
                        user_excluded_pids.add(pid)
                        try:
                            proc = psutil.Process(pid)
                            print(f"Aggiunto alla lista di esclusione: PID {pid} ({proc.name()})")
                        except:
                            print(f"Aggiunto alla lista di esclusione: PID {pid}")
                    else:
                        print(f"Processo non trovato: {target}")
            
            elif cmd.startswith('-') and not cmd.startswith('--'):
                target = cmd[1:].strip()
                if target:
                    pid = get_process_by_name_or_pid(target)
                    if pid and pid in user_excluded_pids:
                        user_excluded_pids.remove(pid)
                        print(f"Rimosso dalla lista di esclusione: PID {pid}")
                    else:
                        print(f"Processo non trovato nella lista di esclusione: {target}")
            
            elif cmd.startswith('--'):
                target = cmd[2:].strip()
                if target:
                    pid = get_process_by_name_or_pid(target)
                    if pid:
                        handle_single_process(pid, "pause", user_excluded_pids)
                    else:
                        print(f"Processo non trovato: {target}")
            
            elif cmd.startswith('++'):
                target = cmd[2:].strip()
                if target:
                    pid = get_process_by_name_or_pid(target)
                    if pid:
                        handle_single_process(pid, "resume", user_excluded_pids)
                    else:
                        print(f"Processo non trovato: {target}")
            
            else:
                print("Comando non riconosciuto. Scrivi 'help' per vedere i comandi disponibili.")

def emergency_recovery_process():
    """
    Processo di ripristino di emergenza.
    Questo viene eseguito come processo separato per garantire il ripristino
    anche in caso di crash del processo principale.
    """
    # Carica i processi in pausa dal file di stato
    paused_processes = load_paused_processes()
    
    if paused_processes:
        print("\n=== RIPRISTINO DI EMERGENZA ===")
        print(f"Trovati {len(paused_processes)} processi da ripristinare.")
        
        for process in paused_processes:
            if process.paused:
                print(f"Ripristino del processo: {process}")
                resume_process(process)
        
        print("Tutti i processi sono stati ripristinati.")
    
    # Pulisci il file di stato
    cleanup_state_file()

def setup_emergency_recovery():
    """
    Configura un processo di ripristino di emergenza che verrà eseguito se il processo principale termina
    in modo anomalo senza ripristinare i processi in pausa.
    """
    # Registra una funzione di pulizia che verrà chiamata all'uscita normale del programma
    atexit.register(cleanup_state_file)
    
    script_path = os.path.abspath(__file__)
    recovery_command = f"python3 {script_path} --recovery"
    
    # Crea un file bash temporaneo per l'esecuzione del comando di ripristino
    recovery_script_path = os.path.join(STATE_DIR, 'recovery.sh')
    with open(recovery_script_path, 'w') as f:
        f.write("#!/bin/bash\n")
        f.write(f"{recovery_command}\n")
    
    # Rendi il file eseguibile
    os.chmod(recovery_script_path, 0o755)
    
    print(f"Meccanismo di ripristino di emergenza configurato: {recovery_script_path}")
    return recovery_script_path

def memory_manager(priority_pid, min_memory_threshold, max_pause_count, check_interval, 
                  force_swap, user_excluded_pids, paused_processes, stop_event, started_event):
    """
    Gestore principale della memoria.
    
    Args:
        priority_pid: PID del processo a cui dare priorità
        min_memory_threshold: Soglia minima di MB per considerare un processo per la pausa
        max_pause_count: Numero massimo di processi da mettere in pausa
        check_interval: Intervallo in secondi tra i controlli
        force_swap: Se True, tenta di forzare lo swap per il processo prioritario
        user_excluded_pids: Set di PID esclusi dall'utente
        paused_processes: Lista di processi messi in pausa (condivisa)
        stop_event: Evento per segnalare la terminazione
        started_event: Evento per segnalare l'avvio del monitoraggio
    """
    # Ottieni informazioni sul processo corrente e la sua catena di genitori
    try:
        current_process = psutil.Process(os.getpid())
        current_app_name = current_process.name()
        parent_chain = get_parent_process_chain(os.getpid())
        parent_chain_pids = [pid for pid, _ in parent_chain]
        parent_chain_pids.append(os.getpid())  # Aggiungi il PID corrente
        
        # Aggiungi anche il processo prioritario alla catena
        priority_parent_chain = get_parent_process_chain(priority_pid)
        for pid, _ in priority_parent_chain:
            if pid not in parent_chain_pids:
                parent_chain_pids.append(pid)
        parent_chain_pids.append(priority_pid)
        
        print("\nProcessi nella catena dei genitori (protetti dalla sospensione):")
        for pid, name in parent_chain:
            print(f"  PID: {pid}, Nome: {name}")
    except Exception as e:
        print(f"Errore nell'ottenere informazioni sul processo corrente: {e}")
        parent_chain_pids = [os.getpid()]
        current_app_name = None
    
    # Attendi il segnale di avvio se richiesto
    if not started_event.is_set():
        print("\nIn attesa del comando 'start' per avviare il monitoraggio...")
        while not started_event.is_set() and not stop_event.is_set():
            time.sleep(0.5)
    
    if stop_event.is_set():
        return
    
    print("\nAvvio del monitoraggio della memoria...")
    last_check_time = 0
    
    try:
        while not stop_event.is_set():
            current_time = time.time()
            if current_time - last_check_time >= check_interval:
                last_check_time = current_time
                
                # Verifica se il processo prioritario è ancora in esecuzione
                try:
                    priority_process = psutil.Process(priority_pid)
                    priority_process_name = priority_process.name()
                except psutil.NoSuchProcess:
                    print(f"\nIl processo prioritario {priority_pid} non è più in esecuzione. Uscita...")
                    stop_event.set()
                    break
                
                # Ottieni informazioni sulla memoria di sistema
                memory_info = get_system_memory_info()
                
                # Stampa le informazioni sulla memoria
                print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Stato della memoria:")
                print(f"  Memoria totale: {memory_info['total']:.2f} MB")
                print(f"  Memoria disponibile: {memory_info['available']:.2f} MB ({memory_info['percent']}% in uso)")
                print(f"  Swap totale: {memory_info['swap_total']:.2f} MB, " +
                      f"Swap in uso: {memory_info['swap_used']:.2f} MB ({memory_info['swap_percent']}%)")
                
                if memory_info['memory_pressure'] is not None:
                    print(f"  Pressione memoria: {memory_info['memory_pressure']}%")
                
                # Stampa le informazioni sul processo prioritario
                try:
                    priority_mem = priority_process.memory_info().rss / (1024 * 1024)
                    priority_cpu = priority_process.cpu_percent(interval=0.1)
                    print(f"\nProcesso Prioritario: PID {priority_pid} ({priority_process_name})")
                    print(f"  Memoria: {priority_mem:.2f} MB, CPU: {priority_cpu:.1f}%")
                    
                    # Se richiesto, tenta di forzare lo swap per il processo prioritario
                    if force_swap and memory_info['percent'] > 80:
                        print("Tentativo di forzare lo swap per il processo prioritario...")
                        force_process_swap(priority_pid)
                except Exception as e:
                    print(f"Errore nell'ottenere informazioni sul processo prioritario: {e}")
                
                # Ottieni informazioni sui processi
                processes = get_processes_memory_usage(priority_pid, parent_chain_pids, user_excluded_pids)
                
                # Aggiorna la lista dei processi in pausa (rimuovi quelli non più esistenti)
                current_paused = []
                for proc in paused_processes:
                    try:
                        if psutil.pid_exists(proc.pid):
                            current_paused.append(proc)
                        else:
                            # Se il processo non esiste più, lo consideriamo non più in pausa
                            print(f"Processo {proc.pid} ({proc.name}) non esiste più, rimosso dalla lista")
                    except:
                        pass
                
                paused_processes[:] = current_paused
                
                # Salva lo stato corrente per il meccanismo di sicurezza
                save_paused_processes([p for p in paused_processes if p.paused])
                
                # Stampa i processi messi in pausa
                current_paused_procs = [p for p in paused_processes if p.paused]
                if current_paused_procs:
                    print("\nProcessi attualmente in pausa:")
                    for proc in current_paused_procs:
                        print(f"  {proc}")
                
                # Verifica se è necessario mettere in pausa altri processi
                if len([p for p in paused_processes if p.paused]) < max_pause_count:
                    high_memory_processes = [
                        p for p in processes 
                        if p.memory_mb >= min_memory_threshold and 
                        not is_essential(p, parent_chain_pids, user_excluded_pids, current_app_name)
                    ]
                    
                    for process in high_memory_processes:
                        if len([p for p in paused_processes if p.paused]) >= max_pause_count or stop_event.is_set():
                            break
                        
                        # Verifica se il processo è già in pausa
                        if any(p.pid == process.pid and p.paused for p in paused_processes):
                            continue
                        
                        print(f"\nMettendo in pausa il processo: {process}")
                        if pause_process(process):
                            # Se è un nuovo processo, lo aggiungiamo alla lista
                            if not any(p.pid == process.pid for p in paused_processes):
                                paused_processes.append(process)
                            # Altrimenti, aggiorniamo lo stato di quello esistente
                            else:
                                for p in paused_processes:
                                    if p.pid == process.pid:
                                        p.paused = True
                                        break
                                        
                            print(f"Processo {process.pid} ({process.name}) messo in pausa con successo")
                            
                            # Aggiorna il file di stato
                            save_paused_processes([p for p in paused_processes if p.paused])
                
                # Stampa i processi che consumano più memoria ma non sono stati messi in pausa
                print("\nProcessi che consumano più memoria (non in pausa):")
                shown_processes = 0
                for process in processes:
                    if shown_processes >= 5:
                        break
                        
                    if not any(p.pid == process.pid and p.paused for p in paused_processes):
                        # Verifica se il processo è essenziale o escluso
                        essential = is_essential(process, parent_chain_pids, user_excluded_pids, current_app_name)
                        status = " (protetto)" if essential else ""
                        print(f"  {process}{status}")
                        shown_processes += 1

def main():
    parser = argparse.ArgumentParser(description='MacOS Memory Priority Manager - Versione avanzata')
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('-n', '--name', help='Nome del processo a cui dare priorità')
    group.add_argument('-p', '--pid', type=int, help='PID del processo a cui dare priorità')
    parser.add_argument('-t', '--threshold', type=float, default=200.0, 
                        help='Soglia minima di memoria (MB) per considerare un processo da mettere in pausa (default: 200 MB)')
    parser.add_argument('-m', '--max-pause', type=int, default=5, 
                        help='Numero massimo di processi da mettere in pausa (default: 5)')
    parser.add_argument('-i', '--interval', type=int, default=10, 
                        help='Intervallo di controllo in secondi (default: 10)')
    parser.add_argument('-s', '--swap', action='store_true',
                        help='Tenta di forzare lo swap per il processo prioritario quando necessario')
    parser.add_argument('-e', '--exclude', nargs='+', default=[],
                        help='Lista di processi (nomi o PID) da escludere dalla sospensione')
    
    args = parser.parse_args()
    
    # Ottieni il PID del processo prioritario
    priority_pid = None
    if args.pid:
        priority_pid = args.pid
    elif args.name:
        priority_pid = get_process_by_name_or_pid(args.name)
    
    if not priority_pid:
        print(f"Errore: Impossibile trovare il processo specificato.")
        sys.exit(1)
    
    # Set di PID esclusi dall'utente
    user_excluded_pids = set()
    
    # Aggiungi i processi specificati dalla riga di comando alla lista di esclusione
    for proc in args.exclude:
        pid = get_process_by_name_or_pid(proc)
        if pid:
            user_excluded_pids.add(pid)
            print(f"Processo escluso dalla sospensione: {proc} (PID: {pid})")
    
    print(f"Dando priorità al processo con PID: {priority_pid}")
    print(f"Soglia di memoria: {args.threshold} MB")
    print(f"Massimo processi in pausa: {args.max_pause}")
    print(f"Intervallo di controllo: {args.interval} secondi")
    print(f"Forzare lo swap: {'Sì' if args.swap else 'No'}")
    
    # Verifica requisiti e dipendenze
    try:
        import psutil
    except ImportError:
        print("\nErrore: il modulo 'psutil' non è installato.")
        print("Installa psutil con: pip install psutil")
        sys.exit(1)
    
    # Evento per la terminazione
    stop_event = threading.Event()
    
    # Avvia il thread per l'input dell'utente
    input_thread = threading.Thread(
        target=input_listener, 
        args=(user_excluded_pids, stop_event),
        daemon=True
    )
    input_thread.start()
    
    try:
        # Avvia il gestore della memoria
        memory_manager(
            priority_pid, 
            args.threshold, 
            args.max_pause, 
            args.interval,
            args.swap,
            user_excluded_pids,
            stop_event
        )
    except KeyboardInterrupt:
        print("\nInterruzione rilevata. Terminazione in corso...")
        stop_event.set()
    
    # Attendi che il thread dell'input termini
    input_thread.join(timeout=2)
    
    print("Programma terminato.")

if __name__ == "__main__":
    # Verifica se lo script è in esecuzione con privilegi amministrativi
    if os.geteuid() != 0:
        print("Attenzione: Questo script richiede privilegi amministrativi per funzionare correttamente.")
        print("Considerare di eseguirlo con 'sudo'.")
    
    main()