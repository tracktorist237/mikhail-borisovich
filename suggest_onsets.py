#!/usr/bin/env python3
"""Offline RMS candidates, NEVER verified speech annotations. No WAV/manifest writes."""
import argparse
from array import array
import json
import math
import sys
import wave
import audio_replay as asr


def suggest(path):
    asr.validate_wav(path)
    rms=[]; frame_size=160  # 10 ms; independent of production detection thresholds
    with wave.open(str(path),'rb') as wav:
        while data:=wav.readframes(frame_size):
            samples=array('h',data)
            if sys.byteorder!='little':samples.byteswap()
            rms.append(math.sqrt(sum(n*n for n in samples)/len(samples)))
    noise=sorted(rms)[len(rms)//5]
    threshold=max(200.,noise*3)
    index=next((i for i in range(len(rms)-2) if all(n>=threshold for n in rms[i:i+3])),None)
    return {'candidate_start_frame':index*frame_size if index is not None else None,
            'candidate_range_frames':[index*frame_size,(index+1)*frame_size] if index is not None else None,
            'verified':False,'confidence':'heuristic-only',
            'reason':'First three consecutive 10-ms frames above max(200, 3 * 20th-percentile RMS); noise may qualify',
            'threshold_rms':round(threshold,2),'noise_estimate_rms':round(noise,2),
            'surrounding_rms':[{'start_frame':i*frame_size,'rms':round(rms[i],2)}
                               for i in range(max(0,index-5),min(len(rms),index+8))] if index is not None else []}


def report(corpus):
    rows=[]
    for clip in corpus.clips:
        path=asr.corpus_file(corpus.root,clip['wav'])
        if asr.sha256_file(path)!=corpus.wav_hashes[clip['id']]:raise asr.CorpusError('WAV changed')
        rows.append({'id':clip['id'],'wav':clip['wav'],'sha256':corpus.wav_hashes[clip['id']],**suggest(path)})
        if asr.sha256_file(path)!=corpus.wav_hashes[clip['id']]:raise asr.CorpusError('WAV changed')
    return {'schema_version':1,'kind':'unverified-onset-suggestions',
            'manifest_sha256':corpus.manifest_sha256,'suggestions':rows}


def cli(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--corpus',required=True);p.add_argument('--report')
    args=p.parse_args(argv)
    try:
        corpus=asr.load_corpus(args.corpus)
        target=asr.report_destination(args.report) if args.report else None
        result=report(corpus)
        for row in result['suggestions']:
            print(row['id'], 'candidate_frame='+str(row['candidate_start_frame']), 'UNVERIFIED')
        if target:
            target.parent.mkdir(parents=True,exist_ok=True)
            with target.open('x') as out:json.dump(result,out,indent=2);out.write('\n')
        return 0
    except (asr.CorpusError,OSError) as exc:
        print('ERROR: '+(str(exc) if isinstance(exc,asr.CorpusError) else type(exc).__name__),file=sys.stderr)
        return 2


if __name__=='__main__':sys.exit(cli())
