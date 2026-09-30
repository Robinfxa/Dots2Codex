"""Private bounded local I/O for native-tool orchestration. No connector calls.

JSON enters on stdin, never as a Python literal. Paths/requests are data, not code.
Input-file chunks are display-only pieces of ONE already-consumed native permit.
"""
import argparse
import json
import os
import stat
import sys
import uuid
from pathlib import Path
from .backend import read_private_file
from .cli import write_new
from .model import canonical, hash_bytes, require, MAX_BYTES
from .session import MAX_STATE


def private_root(path):
    root=Path(path)
    require(not root.is_symlink() and root.is_dir() and root.stat().st_uid==os.getuid() and
            not root.stat().st_mode&0o077,'unsafe_capture_directory')
    return root


def capture(root, raw):
    root=private_root(root)
    require(len(raw)<=MAX_STATE,'capture_size_exceeded')
    value=json.loads(raw)
    data=canonical(value,max_bytes=MAX_STATE)
    path=root/('capture-'+uuid.uuid4().hex+'.json')
    write_new(path,data)
    return {'path':str(path),'bytes':len(data),'sha256':hash_bytes(data)}


def begin_capture(root):
    root=private_root(root)
    path=root/('incoming-'+uuid.uuid4().hex+'.part')
    write_new(path,b'')
    return {'path':str(path),'bytes':0}


def append_capture(root,path,offset,raw):
    root=private_root(root);path=Path(path)
    require(path.parent.resolve()==root.resolve() and path.name.startswith('incoming-') and
            path.suffix=='.part','invalid_capture_path')
    require(len(raw)<=65536 and type(offset) is int and 0<=offset<=MAX_STATE-len(raw),'capture_size_exceeded')
    old=read_private_file(path,MAX_STATE)
    require(len(old)==offset,'capture_append_offset_mismatch')
    fd=os.open(path,os.O_WRONLY|os.O_APPEND|os.O_NOFOLLOW)
    try:
        with os.fdopen(fd,'wb',closefd=False) as out:out.write(raw);out.flush();os.fsync(fd)
    finally:os.close(fd)
    return {'path':str(path),'bytes':offset+len(raw)}


def seal_capture(root,path):
    root=private_root(root);path=Path(path)
    require(path.parent.resolve()==root.resolve() and path.name.startswith('incoming-') and
            path.suffix=='.part','invalid_capture_path')
    return capture(root,read_private_file(path,MAX_STATE))


def unwrap_capture(root,path):
    root=private_root(root)
    wrapper=json.loads(read_private_file(Path(path),MAX_STATE))
    require(isinstance(wrapper,dict) and set(wrapper)=={'stage','context','response'} and
            isinstance(wrapper['stage'],str) and isinstance(wrapper['context'],dict),'invalid_capture_envelope')
    # The original envelope already durably associates even error/unknown tool
    # responses with the exact attempted operation. Keep it for reconciliation.
    return capture(root,canonical(wrapper['response'],max_bytes=MAX_STATE))


def materialize(root, source):
    root=private_root(root)
    fd=os.open(source,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    try:
        st=os.fstat(fd)
        require(stat.S_ISREG(st.st_mode) and st.st_uid==os.getuid() and st.st_size<=MAX_BYTES,
                'unsafe_downloaded_file')
        with os.fdopen(fd,'rb',closefd=False) as stream: raw=stream.read(MAX_BYTES+1)
    finally: os.close(fd)
    require(len(raw)<=MAX_BYTES,'downloaded_file_too_large')
    path=root/('download-'+uuid.uuid4().hex+'.json')
    write_new(path,raw)
    return {'path':str(path),'bytes':len(raw),'sha256':hash_bytes(raw)}


def packet_chunk(path, expected_sha256=None, offset=0, max_chars=16384):
    require(type(offset) is int and offset>=0 and type(max_chars) is int and 1<=max_chars<=16384,
            'invalid_packet_chunk_range')
    raw=read_private_file(Path(path),MAX_STATE)
    digest=hash_bytes(raw)
    require(expected_sha256 is None and offset==0 or expected_sha256==digest,'packet_file_hash_mismatch')
    text=raw.decode('utf-8');require(offset<=len(text),'invalid_packet_chunk_range')
    end=min(offset+max_chars,len(text))
    def packet(end):
        return {'text':text[offset:end],'offset_chars':offset,'next_offset_chars':end,
                'total_chars':len(text),'eof':end==len(text),'sha256':digest}
    # Bound serialized OUTPUT bytes as well as character count, including JSON
    # escaping and worst-case Unicode. The native shell cap is token-based; a
    # 20k token budget is conservative for at most 16k bytes of JSON output.
    while len(json.dumps(packet(end),ensure_ascii=False).encode('utf-8'))>16000:
        end=offset+(end-offset)//2
    return packet(end)


def input_chunk(path, expected_sha256, offset=0, max_chars=12000):
    require(type(offset) is int and offset>=0 and type(max_chars) is int and 1<=max_chars<=16000,
            'invalid_input_chunk_range')
    raw=read_private_file(Path(path),MAX_STATE)
    require(hash_bytes(raw)==expected_sha256,'input_file_hash_mismatch')
    value=json.loads(raw)
    require(value.get('action')=='native_inference_once' and value.get('no_automatic_retry') is True,
            'consumed_input_packet_required')
    text=raw.decode('utf-8')
    require(offset<=len(text),'invalid_input_chunk_range')
    end=min(offset+max_chars,len(text))
    return {'text':text[offset:end],'offset_chars':offset,'next_offset_chars':end,
            'total_chars':len(text),'eof':end==len(text),'sha256':expected_sha256,
            'display_only':True,'new_native_permit':False}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('operation',choices=['capture','capture-begin','capture-append','capture-seal','unwrap-capture','materialize','input-chunk','packet-chunk'])
    p.add_argument('--root');p.add_argument('--source');p.add_argument('--path');p.add_argument('--sha256')
    p.add_argument('--offset',type=int,default=0);p.add_argument('--max-chars',type=int,default=12000)
    a=p.parse_args();os.umask(0o077)
    if a.operation=='capture':value=capture(a.root,sys.stdin.buffer.read(MAX_STATE+1))
    elif a.operation=='capture-begin':value=begin_capture(a.root)
    elif a.operation=='capture-append':value=append_capture(a.root,a.path,a.offset,sys.stdin.buffer.read(65537))
    elif a.operation=='capture-seal':value=seal_capture(a.root,a.path)
    elif a.operation=='unwrap-capture':value=unwrap_capture(a.root,a.path)
    elif a.operation=='materialize':value=materialize(a.root,a.source)
    elif a.operation=='packet-chunk':value=packet_chunk(a.path,a.sha256,a.offset,a.max_chars)
    else:value=input_chunk(a.path,a.sha256,a.offset,a.max_chars)
    print(json.dumps(value,ensure_ascii=False))


if __name__=='__main__':main()
