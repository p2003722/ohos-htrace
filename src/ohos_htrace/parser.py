import struct
from typing import List, Dict, Any

from .generated import common_types_pb2
from .generated import trace_plugin_result_pb2

HEADER_SIZE = 1024
HEADER_MAGIC = 0x464F5250534F484F

class HtraceParser:
    def __init__(self, filepath: str):
        self.filepath = filepath
        self.print_events: List[Dict[str, Any]] = []

    def parse(self) -> List[Dict[str, Any]]:
        with open(self.filepath, 'rb') as f:
            # 1. Read and validate Header
            header_data = f.read(HEADER_SIZE)
            if len(header_data) < HEADER_SIZE:
                raise ValueError("File is too small to contain a valid header.")
                
            magic = struct.unpack_from('<Q', header_data, 0)[0]
            if magic != HEADER_MAGIC:
                raise ValueError(f"Invalid magic number: {hex(magic)}. Expected {hex(HEADER_MAGIC)}.")
            
            # 2. Read Segments
            while True:
                # Read 4 bytes length
                length_bytes = f.read(4)
                if not length_bytes:
                    break # EOF
                
                if len(length_bytes) < 4:
                    break
                    
                segment_length = struct.unpack('<I', length_bytes)[0]
                
                # Check for multiple headers (concatenated traces)
                if segment_length == 0x534F484F:  # 'OHOS' part of 'OHOSPROF'
                    f.read(HEADER_SIZE - 4)
                    continue
                    
                # Read payload
                payload = f.read(segment_length)
                if len(payload) < segment_length:
                    break
                    
                self._parse_segment(payload)
                
        return self.print_events
        
    def _parse_segment(self, payload: bytes):
        plugin_data = common_types_pb2.ProfilerPluginData()
        try:
            plugin_data.ParseFromString(payload)
        except Exception:
            return
            
        if plugin_data.name == "ftrace-plugin":
            self._parse_ftrace_data(plugin_data.data)
            
    def _parse_ftrace_data(self, data: bytes):
        plugin_result = trace_plugin_result_pb2.TracePluginResult()
        try:
            plugin_result.ParseFromString(data)
        except Exception:
            return
            
        # Iterate over ftrace_cpu_detail
        for cpu_detail in plugin_result.ftrace_cpu_detail:
            for event in cpu_detail.event:
                if event.HasField('print_format'):
                    self._handle_print_event(event)
                elif event.HasField('task_rename_format'):
                    self._handle_task_rename(event)
                elif event.HasField('sched_switch_format'):
                    self._handle_sched_switch(event)

        # Extract comm_dict for thread name resolution
        if hasattr(plugin_result, 'comm_dict'):
            for entry in plugin_result.comm_dict:
                self.print_events.append({
                    'type': 'rename',
                    'pid': entry.tid,
                    'comm': entry.comm,
                    'ts': 0,
                })

    def _handle_task_rename(self, event):
        pid = event.task_rename_format.pid
        newcomm = event.task_rename_format.newcomm
        
        self.print_events.append({
            'type': 'rename',
            'pid': pid,
            'comm': newcomm,
            'ts': event.timestamp
        })

    def _handle_sched_switch(self, event):
        """Handle sched_switch events — used for thread/process creation and naming."""
        ts = event.timestamp
        ss = event.sched_switch_format

        # Record both prev and next threads for name resolution
        if ss.prev_pid and ss.prev_comm:
            self.print_events.append({
                'type': 'sched_thread',
                'ts': ts,
                'pid': ss.prev_pid,
                'comm': ss.prev_comm,
            })
        if ss.next_pid and ss.next_comm:
            self.print_events.append({
                'type': 'sched_thread',
                'ts': ts,
                'pid': ss.next_pid,
                'comm': ss.next_comm,
            })

    def _handle_print_event(self, event):
        timestamp = event.timestamp
        event_tgid = event.tgid
        comm = event.comm
        
        # common_fields has pid (thread id)
        pid = event.common_fields.pid if event.HasField('common_fields') else -1
        
        # extract print buffer
        buf = event.print_format.buf

        # ── KEY FIX: extract tgid from buf, matching C++ streamer behavior ──
        # The buf format is "B|<tgid>|<name>", "E|<tgid>", "S|<tgid>|<name>|<cookie>", etc.
        # The tgid in the buf is the authoritative process ID, not event.tgid
        # (event.tgid may still reflect the parent process before fork)
        buf_stripped = buf.strip()
        parts = buf_stripped.split('|')
        buf_tgid = None
        if len(parts) > 1:
            try:
                buf_tgid = int(parts[1])
            except (ValueError, IndexError):
                pass

        tgid = buf_tgid if buf_tgid is not None else event_tgid
        
        self.print_events.append({
            'type': 'print',
            'ts': timestamp,
            'pid': pid,    # thread id
            'tgid': tgid,  # process id (from buf, matching streamer)
            'comm': comm,  # name
            'buf': buf
        })
