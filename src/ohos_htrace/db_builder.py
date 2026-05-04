import sqlite3

def build_memory_db(events: list[dict]) -> sqlite3.Connection:
    conn = sqlite3.connect(':memory:')
    cur = conn.cursor()
    
    # Create tables exactly as expected by trace_analyzer.py
    cur.execute('''CREATE TABLE process (id INTEGER PRIMARY KEY, name TEXT, pid INTEGER)''')
    cur.execute('''CREATE TABLE thread (id INTEGER PRIMARY KEY, ipid INTEGER, name TEXT, tid INTEGER)''')
    cur.execute('''CREATE TABLE callstack (id INTEGER PRIMARY KEY, callid INTEGER, parent_id INTEGER, name TEXT, depth INTEGER, ts INTEGER, dur INTEGER)''')
    cur.execute('''CREATE TABLE frame_slice (id INTEGER PRIMARY KEY, vsync INTEGER, ts INTEGER, dur INTEGER, type_desc TEXT, flag INTEGER, ipid INTEGER)''')
    
    processes = {}  # tgid -> ipid
    threads = {}    # (tgid, pid) -> itid
    
    ipid_counter = 1
    itid_counter = 1
    callstack_counter = 1
    
    process_names = {}
    thread_names = {}

    # ── Collect sched_thread names for better thread naming ──
    sched_names = {}  # pid -> comm
    for e in events:
        if e['type'] == 'sched_thread':
            pid = e['pid']
            comm = e.get('comm', '')
            if comm and pid not in sched_names:
                sched_names[pid] = comm

    # First pass: map PIDs/TIDs and their names
    for e in events:
        if e['type'] == 'rename':
            pid = e['pid']
            process_names[pid] = e['comm']
            thread_names[pid] = e['comm']
            continue

        if e['type'] != 'print':
            continue
            
        tgid = e['tgid']
        pid = e['pid']
        comm = e.get('comm', '')
        
        if tgid == pid and comm:
            if tgid not in process_names:
                process_names[tgid] = comm
        elif comm:
            if pid not in thread_names:
                thread_names[pid] = comm
            
        if tgid not in processes:
            processes[tgid] = ipid_counter
            ipid_counter += 1
            
        if (tgid, pid) not in threads:
            threads[(tgid, pid)] = itid_counter
            itid_counter += 1

    # Merge sched_names into thread_names (lower priority than print/rename names)
    for pid, comm in sched_names.items():
        if pid not in thread_names:
            thread_names[pid] = comm
            
    # Insert processes
    for tgid, ipid in processes.items():
        name = process_names.get(tgid, f"Process {tgid}")
        cur.execute('INSERT INTO process (id, name, pid) VALUES (?, ?, ?)', (ipid, name, tgid))
        
    # Insert threads
    for (tgid, pid), itid in threads.items():
        ipid = processes[tgid]
        name = thread_names.get(pid, process_names.get(tgid, f"Thread {pid}"))
        cur.execute('INSERT INTO thread (id, ipid, name, tid) VALUES (?, ?, ?, ?)', (itid, ipid, name, pid))
        
    # Second pass: build callstack from B/E/S/F events
    # ── CRITICAL: sort by (thread, timestamp) to ensure correct B/E pairing ──
    # Events from different CPU cores arrive interleaved; without sorting,
    # B/E events for the same thread can be mis-paired across time gaps.
    print_events = [e for e in events if e['type'] == 'print']
    print_events.sort(key=lambda e: (e['tgid'], e['pid'], e['ts']))

    thread_stacks = {}   # itid -> list of node dicts (for B/E sync events)
    async_slices = {}    # (itid, cookie) -> node dict (for S/F async events)
    callstack_inserts = []
    
    for e in print_events:
            
        tgid = e['tgid']
        pid = e['pid']
        key = (tgid, pid)
        if key not in threads:
            continue
        itid = threads[key]
        
        buf = e['buf'].strip()
        parts = buf.split('|')
        if not parts:
            continue
            
        action = parts[0]
        
        if action == 'B':
            # ── FIX: preserve full name after B|tgid| (including |I39 metadata) ──
            # Format: B|tgid|name  or  B|tgid|name|metadata
            name = '|'.join(parts[2:]) if len(parts) > 2 else ""
            if itid not in thread_stacks:
                thread_stacks[itid] = []
                
            stack = thread_stacks[itid]
            depth = len(stack)
            parent_id = stack[-1]['id'] if depth > 0 else None
            
            node = {
                'id': callstack_counter,
                'callid': itid,
                'parent_id': parent_id,
                'name': name,
                'depth': depth,
                'ts': e['ts'],
                'dur': -1 
            }
            callstack_counter += 1
            stack.append(node)
            callstack_inserts.append(node)
            
        elif action == 'E':
            if itid in thread_stacks and len(thread_stacks[itid]) > 0:
                node = thread_stacks[itid].pop()
                node['dur'] = e['ts'] - node['ts']

        elif action == 'S':
            # Async start: S|tgid|name cookie  or  S|tgid|name|cookie|...
            if len(parts) > 2:
                # Extract name and cookie — name ends at space, cookie follows
                name_and_rest = '|'.join(parts[2:])
                # In streamer, name stops at space, value is after space
                space_idx = name_and_rest.find(' ')
                if space_idx >= 0:
                    name = name_and_rest[:space_idx]
                    try:
                        cookie = int(name_and_rest[space_idx + 1:].split('|')[0])
                    except (ValueError, IndexError):
                        cookie = 0
                else:
                    # Fallback: try pipe-separated
                    sub = name_and_rest.split('|')
                    name = sub[0]
                    try:
                        cookie = int(sub[1]) if len(sub) > 1 else 0
                    except ValueError:
                        cookie = 0

                node = {
                    'id': callstack_counter,
                    'callid': itid,
                    'parent_id': None,
                    'name': name,
                    'depth': 0,
                    'ts': e['ts'],
                    'dur': -1,
                }
                callstack_counter += 1
                async_slices[(itid, cookie)] = node
                callstack_inserts.append(node)

        elif action == 'F':
            # Async finish: F|tgid|name cookie
            if len(parts) > 2:
                name_and_rest = '|'.join(parts[2:])
                space_idx = name_and_rest.find(' ')
                if space_idx >= 0:
                    try:
                        cookie = int(name_and_rest[space_idx + 1:].split('|')[0])
                    except (ValueError, IndexError):
                        cookie = 0
                else:
                    sub = name_and_rest.split('|')
                    try:
                        cookie = int(sub[1]) if len(sub) > 1 else 0
                    except ValueError:
                        cookie = 0

                akey = (itid, cookie)
                if akey in async_slices:
                    node = async_slices.pop(akey)
                    node['dur'] = e['ts'] - node['ts']
                
    # Insert callstack
    insert_data = []
    for node in callstack_inserts:
        if node['dur'] < 0:
            node['dur'] = 0 # Prevent negative duration
        insert_data.append((node['id'], node['callid'], node['parent_id'], node['name'], node['depth'], node['ts'], node['dur']))
        
    cur.executemany('INSERT INTO callstack (id, callid, parent_id, name, depth, ts, dur) VALUES (?, ?, ?, ?, ?, ?, ?)', insert_data)
    
    conn.commit()
    return conn
