"""本地真实录像与可独立判断胜负的最小录像验收，不更改源录像。"""
import argparse
import hashlib
import json
import struct
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bot.replay_analysis import analyze, write_json
from bot.replay_analysis_ui import render_report


def deckout_fixture(path):
    body = b''.join(n.encode('utf-16le').ljust(40,b'\0') for n in ('先手空牌库','后手正常牌库'))
    body += struct.pack('<4I',8000,0,1,(5<<16)|0x10)
    for main in ([],[89631139]*40):
        body += struct.pack('<I',len(main)) + struct.pack('<'+'I'*len(main),*main) + struct.pack('<I',0)
    # 空连锁窗口也要求录像里有显式的“不发动”响应。
    for value in (7,-1,-1,-1,7,-1): body += b'\x04' + struct.pack('<i',value)
    header = struct.pack('<6I8s8I4I',0x32707279,0x1362,0x10,0,len(body),1792836000,bytes(8),*range(1,9),1,0,0,0)
    path.write_bytes(header+body)


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--replay-root',type=Path,required=True)
    p.add_argument('--game-root',type=Path,default=Path('D:/MyCardLibrary/ygopro'))
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--limit',type=int,default=24)
    p.add_argument('--allow-unsupported',action='store_true',help='兼容性审计时允许记录明确拒绝的旧录像，不将其视为成功解析')
    a=p.parse_args()
    a.output.mkdir(parents=True,exist_ok=True)
    summary=[]
    with tempfile.TemporaryDirectory() as temp:
        f=Path(temp)/'known-deckout.yrp'; deckout_fixture(f)
        report=analyze(f,a.game_root)
        write_json(a.output/'known-deckout.json',report)
        assert report['playback']=='completed',report['warnings']
        assert report['winner']==1 and report['first_player']==0,report
        assert report['responses_consumed']==6
    paths=sorted(a.replay_root.rglob('*.yrp'),key=lambda f:f.stat().st_mtime,reverse=True)
    seen=set()
    for path in paths:
        digest=hashlib.sha256(path.read_bytes()).hexdigest()
        if digest in seen:continue
        seen.add(digest)
        report=analyze(path,a.game_root)
        # 相同输入重复重放，检查先后手、胜负和所有用卡事件是否稳定。
        repeated=analyze(path,a.game_root)
        for key in ('first_player','winner','events','responses_consumed','playback'):
            assert report.get(key)==repeated.get(key),(path,key)
        write_json(a.output/(digest+'.json'),report)
        summary.append({'file':str(path),'digest':digest,'playback':report['playback'],
            'first_player':report['first_player'],'winner':report['winner'],
            'responses':report['responses_consumed'],'events':len(report['events']),
            'warnings':report['warnings']})
        if len(summary)==1:
            body=render_report(report,'preview-token',1)
            page='<!doctype html><meta charset="utf-8"><title>小局解析验收</title><style>body{font:16px Microsoft YaHei;background:#f3f6fb;color:#172b4d;max-width:1100px;margin:32px auto}section{background:white;padding:24px;margin:20px 0;border-radius:16px}td,th{padding:9px;border-bottom:1px solid #ddd;text-align:left}table{width:100%}details{margin:16px 0}a{color:#4263eb}.note{color:#87620c}</style>'+body
            (a.output/'sample.html').write_text(page,encoding='utf-8')
        print(json.dumps(summary[-1],ensure_ascii=True),flush=True)
        if len(summary)>=a.limit:break
    write_json(a.output/'summary.json',{'known_deckout':'passed','repeat_consistency':'passed',
        'supported':sum(r['playback']!='failed' for r in summary),
        'unsupported':sum(r['playback']=='failed' for r in summary),'replays':summary})
    if not a.allow_unsupported:
        assert all(r['playback']!='failed' for r in summary),'存在未兼容录像，请查看summary.json'


if __name__=='__main__':main()
