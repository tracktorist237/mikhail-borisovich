#!/usr/bin/env python3
"""Sample a process tree without reading arguments, browser state or audio."""
import argparse
import json
import os
from pathlib import Path
import time


def snapshot(root):
    all_processes = {}
    for path in Path('/proc').glob('[0-9]*/stat'):
        try:
            raw = path.read_text()
            parts = raw[raw.rfind(')')+2:].split()
            all_processes[int(path.parent.name)] = (
                int(parts[1]), int(parts[11])+int(parts[12]),
                int(parts[21])*os.sysconf('SC_PAGE_SIZE'), int(parts[19]),
                raw[raw.find('(')+1:raw.rfind(')')])
        except (OSError, ValueError, IndexError):
            continue
    selected = {root}
    while True:
        children = {pid for pid, p in all_processes.items() if p[0] in selected}
        grown = selected | children
        if grown == selected:
            break
        selected = grown
    return {pid: all_processes[pid] for pid in selected if pid in all_processes}


def system_memory():
    """System-wide swap usage; zram physical RAM is distinct from logical data."""
    swap_used = zram_used = 0
    for line in Path('/proc/swaps').read_text().splitlines()[1:]:
        fields = line.split()
        if fields[0].startswith('/dev/zram'):
            zram_used += int(fields[3]) * 1024
        else:
            swap_used += int(fields[3]) * 1024
    zram_ram = 0
    for path in Path('/sys/block').glob('zram*/mm_stat'):
        zram_ram += int(path.read_text().split()[2])
    return {'system_swap_used_mib': swap_used / 1024**2,
            'system_zram_used_mib': zram_used / 1024**2,
            'system_zram_ram_mib': zram_ram / 1024**2}


def firefox_count(processes):
    # Include Firefox subprocesses (Web Content, RDD, Socket, etc.) by ancestry.
    selected = {pid for pid, p in processes.items() if p[4] in ('firefox', 'firefox-bin')}
    while True:
        grown = selected | {pid for pid, p in processes.items() if p[0] in selected}
        if grown == selected:
            return len(selected)
        selected = grown


def measure(pid, seconds):
    started = time.monotonic()
    initial = snapshot(pid)
    if pid not in initial:
        raise ValueError('Root process does not exist')
    previous = initial
    cpu_ticks = 0
    rss = []
    count = []
    firefox = []
    memory = [system_memory()]
    while time.monotonic()-started < seconds:
        time.sleep(min(1, max(.01, seconds-(time.monotonic()-started))))
        current = snapshot(pid)
        if pid not in current or current[pid][3] != initial[pid][3]:
            raise RuntimeError('Measured process exited; sample is incomplete')
        for process, p in current.items():
            old = previous.get(process)
            cpu_ticks += max(0, p[1] - old[1]) if old and old[3] == p[3] else p[1]
        rss.append(sum(p[2] for p in current.values())/1024**2)
        count.append(len(current))
        firefox.append(firefox_count(current))
        memory.append(system_memory())
        previous = current
    elapsed = time.monotonic()-started
    return dict(seconds=round(elapsed,1), cpu_one_core_percent=round(
        cpu_ticks/os.sysconf('SC_CLK_TCK')/elapsed*100,1),
        rss_mean_mib=round(sum(rss)/len(rss),1), rss_max_mib=round(max(rss),1),
        processes_max=max(count), firefox_processes_max=max(firefox),
        **{key+'_max':round(max(sample[key] for sample in memory),2)
           for key in memory[0]})


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pid',type=int,required=True)
    parser.add_argument('--seconds',type=float,default=20)
    parser.add_argument('--label',required=True)
    args=parser.parse_args()
    if args.seconds <= 0: parser.error('seconds must be positive')
    print(json.dumps({'label':args.label,**measure(args.pid,args.seconds)},ensure_ascii=False))
