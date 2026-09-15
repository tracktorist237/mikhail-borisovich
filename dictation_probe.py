#!/usr/bin/env python3
"""Reproduce dictation UI behaviour. Never sends a ChatGPT message."""
import argparse
from contextlib import ExitStack
import time
from chatgpt_ui import ChatGPTUI
from main import config
from dictation_audio import graph, input_routes, InputMeter, temporary_mic_boost


# Only rendered controls and text; no browser storage or application internals.
DETAIL_SCRIPT = """
const visible=e=>e.getClientRects().length && getComputedStyle(e).visibility!=='hidden';
const controls=[...document.querySelectorAll('main button,main [role="button"]')].filter(visible);
return {
  controls:controls.map(e=>({name:e.getAttribute('aria-label')||e.innerText,
      title:e.getAttribute('title'), disabled:!!e.disabled, testid:e.getAttribute('data-testid')})),
  editors:[...document.querySelectorAll('main textarea,main [contenteditable="true"],main [role="textbox"]')]
      .filter(visible).map(e=>({role:e.getAttribute('role'),label:e.getAttribute('aria-label'),
          text:e.value===undefined?e.innerText:e.value})),
  status:[...document.querySelectorAll('[role="status"],[role="alert"],[role="dialog"],[aria-busy="true"],[role="progressbar"]')]
      .filter(visible).map(e=>({role:e.getAttribute('role'), label:e.getAttribute('aria-label'),text:e.innerText})),
  main_text:document.querySelector('main')?.innerText.slice(0,3000)
};
"""


def observe_transcription(ui, seconds):
    started = time.monotonic()
    previous = None
    while True:
        state = ui.snapshot()
        detail = ui.driver.execute_script(DETAIL_SCRIPT)
        sample = {k:state[k] for k in ('recording','dictate','composer','send','alerts')}
        sample['visible_dom'] = detail
        if sample != previous:
            print(f'[POST STOP +{time.monotonic()-started:.1f}s]', sample, flush=True)
            previous = sample
        if time.monotonic()-started >= seconds:
            print('OBSERVATION COMPLETE; no message sent.', flush=True)
            return state
        time.sleep(1)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--method',choices=['native','dom','bidi','keyboard','scheduled','os-keyboard','manual'],default='manual')
    parser.add_argument('--seconds',type=float,default=3)
    parser.add_argument('--internal-mic-boost',type=int,choices=range(4),default=None,
                        help='Temporary ALSA card 0 Internal Mic Boost; restored on exit')
    parser.add_argument('--discard-result',action='store_true',help='Clear only the transcript produced by this diagnostic')
    parser.add_argument('--clear-test-draft',action='store_true')
    parser.add_argument('--trace-stop',action='store_true')
    parser.add_argument('--observe-seconds',type=float,default=60)
    args=parser.parse_args()
    if not 1 <= args.observe_seconds <= 180:parser.error('observe-seconds must be between 1 and 180')
    if not 1 <= args.seconds <= 45:parser.error('seconds must be between 1 and 45')
    cfg=config()
    cfg['dictation_stop_method']='native' if args.method=='manual' else args.method
    ui=ChatGPTUI(cfg)
    meters=[]
    cleanup=ExitStack()
    try:
        ui.open()
        if args.clear_test_draft:
            from selenium.webdriver.common.keys import Keys
            draft = ui.snapshot()['composer']
            if draft:
                if draft not in ('Сколько будет два плюс два?', 'zoudei vil поговорить про...'):
                    raise RuntimeError('Unexpected draft; left unchanged')
                editor = ui.driver.find_element('css selector', '#prompt-textarea[contenteditable="true"]')
                editor.send_keys(Keys.CONTROL, 'a')
                editor.send_keys(Keys.BACKSPACE)
                print('Previous diagnostic draft cleared through composer.',flush=True)
        cleanup.enter_context(temporary_mic_boost(args.internal_mic_boost))
        if args.method == 'scheduled':
            ui.driver.execute_script("""
                const delay=arguments[0]*1000, expires=Date.now()+60000;
                let seen=null;
                const timer=setInterval(()=>{
                    if(Date.now()>expires){clearInterval(timer);return;}
                    const nodes=[...document.querySelectorAll('main button[aria-label="Stop dictation"]')]
                        .filter(e=>e.getClientRects().length && !e.disabled);
                    if(nodes.length!==1)return;
                    if(seen===null)seen=Date.now();
                    if(Date.now()-seen>=delay){clearInterval(timer);nodes[0].click();}
                },250);
            """, args.seconds)
            print('Scheduled DOM Stop armed before recording.',flush=True)
        print(ui.start_dictation()['detail'],flush=True)
        if not args.trace_stop:
            routes = input_routes(graph(), ui.driver.capabilities['moz:processID'])
            print('FIREFOX INPUT ROUTES:', routes, flush=True)
            sources = {r['serial'] for r in routes if r['serial'] is not None
                       and r['media_class'] == 'Audio/Source' and not r['monitor']}
            for serial in sources:
                meters.append(InputMeter(serial))
            if not sources:
                print('WARNING: no directly linked microphone source found; levels not measured.', flush=True)
        if args.method=='manual':
            print('Произнесите короткую фразу и вручную нажмите Stop dictation в Firefox. '
                  'Этот тест ничего не отправляет.',flush=True)
            input('После ручного Stop сразу нажмите Enter здесь: наблюдаю DOM ещё '
                  f'{args.observe_seconds:g} секунд, затем закрываю окно: ')
            for meter in meters:
                meter.close()
            meters.clear()
        elif args.method=='os-keyboard':
            time.sleep(args.seconds)
            print('OS KEY BEGIN',flush=True)
            print(ui.stop_dictation()['detail'],flush=True)
            print('OS KEY END',flush=True)
            time.sleep(2)
        elif args.method=='scheduled':
            time.sleep(args.seconds+3)
            print('Inspecting scheduled DOM Stop result.',flush=True)
        else:
            time.sleep(args.seconds)
            print(f'STOP BEGIN method={args.method}',flush=True)
            if args.trace_stop:
                original_execute = ui.driver.execute
                def traced(command, params=None):
                    started=time.monotonic()
                    print(f'[WEBDRIVER BEGIN] {command}',flush=True)
                    try:
                        return original_execute(command, params)
                    finally:
                        print(f'[WEBDRIVER END] {command} {time.monotonic()-started:.2f}s',flush=True)
                ui.driver.execute=traced
                ui.driver.command_executor.client_config.timeout=15
            print(ui.stop_dictation()['detail'],flush=True)
        state = observe_transcription(ui, args.observe_seconds)
        if args.discard_result and state['composer']:
            from selenium.webdriver.common.keys import Keys
            if ui.snapshot()['composer'] != state['composer']:
                raise RuntimeError('Composer changed after observation; left unchanged')
            editor=ui.driver.find_element('css selector','#prompt-textarea[contenteditable="true"]')
            editor.send_keys(Keys.CONTROL,'a')
            editor.send_keys(Keys.BACKSPACE)
            print('Diagnostic transcript cleared; nothing sent.',flush=True)
    except Exception as error:
        print(f'PROBE FAILED: {type(error).__name__}: {error}', flush=True)
        if ui.driver:
            try:
                state = ui.snapshot()
                print('UI AT FAILURE:', {k:state[k] for k in (
                    'recording','dictate','composer','send','alerts')}, flush=True)
                buttons = ui.driver.execute_script("""
                    return [...document.querySelectorAll('button,[role="button"]')]
                        .filter(e=>e.getClientRects().length && getComputedStyle(e).visibility!=='hidden')
                        .map(e=>({name:e.getAttribute('aria-label')||e.innerText,
                                  disabled:!!e.disabled}));
                """)
                print('VISIBLE CONTROLS:', buttons, flush=True)
            except Exception as diagnostic_error:
                print('UI diagnostic failed:', diagnostic_error, flush=True)
        raise
    finally:
        try:
            for meter in meters:
                meter.close()
            ui.close()
        finally:
            cleanup.close()


if __name__=='__main__':main()
