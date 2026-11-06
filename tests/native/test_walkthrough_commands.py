"""Real native document rendering, versions and guarded async command work."""
import os
import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SETUP = r'''
import json, sys, threading, time, wave
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
class NoGui:
    def find_spec(self, name, *_args):
        if name.split('.')[0] in ('PyQt6', 'ui_qt'):
            raise AssertionError('Forbidden GUI import: ' + name)
sys.meta_path.insert(0, NoGui())
from PIL import Image
from config import data_root
from services.desktop_runtime import QCoreApplication
from services.settings import settings_manager
from services.native_desktop.ui import NativeUIController
from services.native_desktop.bootstrap import NativeContext
from services.native_desktop.commands import dispatch
from services.application_controller import ApplicationController
from services.database import db
from services.walkthrough import library, finish, document, write_up
from services.walkthrough.session import WalkthroughSession, Note
from transcriber import LocalWhisperBackend
from transcriber.base import TranscriptionBackend
settings_manager.update_settings({'selected_model':'api','auto_paste':False,'copy_clipboard':False,'walkthrough_write_up':False})
app=QCoreApplication([])
events=[]
class Clipboard:
    def __init__(self): self.copies=[]
    def copy_files_to_clipboard(self, files, text='', image=None):
        self.copies.append((list(files),text,image));return True
    def discard_clipboard_prefetch(self): pass
    def close(self): pass
clipboard=Clipboard()
ui=NativeUIController(lambda event,data:events.append((event,data)),clipboard=clipboard)
controller=ApplicationController(ui,local_backend=LocalWhisperBackend(load=False))
controller._startup_database_ready=True
db.ensure_initialized()
class Backend(TranscriptionBackend):
    def __init__(self):
        super().__init__();self.calls=0;self.entered=threading.Event();self.release=None
    def transcribe(self,path):
        self.is_transcribing=True
        self.entered.set()
        if self.release is not None: assert self.release.wait(5)
        self.calls+=1;self.is_transcribing=False;return f'Recovered words {self.calls}'
    def is_available(self): return True
    def cancel_transcription(self):
        super().cancel_transcription()
        if self.release is not None:self.release.set()
backend=Backend()
controller.current_backend=backend
controller.transcription_backends['test']=backend
controller._current_model_name='api'
context=NativeContext(controller,ui,ui.emit,lambda:None)
context.captures_root=Path(data_root())/'captures'
context.captures_root.mkdir(exist_ok=True)
folder=context.captures_root/'2026-11-01_10-00-00'
folder.mkdir()
session=WalkthroughSession(started_at=datetime(2026,11,1,10),make_folder=lambda _:folder)
session.add_shot(Image.new('RGB',(90,60),'red'),1.)
session.add_shot(Image.new('RGB',(90,60),'blue'),2.)
shots=session.saved_shots()
finish.build_documents(session,shots,['First','Second','Tail'],['First','Second','Tail'],duration_s=3,
    notes=[Note('first note',0),Note('second note',1),Note('tail note',2)])
session.end()
def call(method,**params):return dispatch(context,method,params)
def settled(record_id=folder.name):
    deadline=time.monotonic()+10
    while time.monotonic()<deadline:
        app.processEvents()
        operation=ui.state.get('walkthrough_operations',{}).get(record_id,{})
        if operation.get('status') in ('done','failed','canceled'):
            return next(data for event,data in reversed(events)
                if event=='walkthroughs.operation' and data.get('job_id')==operation.get('job_id')
                and data.get('status')==operation['status'])
        time.sleep(.005)
    raise AssertionError(('Timed out',ui.state))
def good(record_id=folder.name):
    operation=settled(record_id)
    assert operation['status']=='done',operation
    assert not controller._walkthrough_library_busy
    return operation
'''


def run_native(source, tmp_path):
    code = SETUP + "\ntry:\n" + textwrap.indent(textwrap.dedent(source), "    ") + "\nfinally:\n    controller.cleanup()\n"
    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT,
        env=dict(os.environ, OPENWHISPER_DESKTOP_RUNTIME="native", OPENWHISPER_DATA_DIR=str(tmp_path), HF_HUB_OFFLINE="1"),
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


def test_review_reorder_preserves_text_screenshot_and_held_note_associations(tmp_path):
    run_native(r'''
        result=call('walkthroughs.review',id=folder.name,texts=['Second edited','First edited','Tail edited'],
            keep=[True,False,False],order=[1,0,2])
        assert result['operation']['status']=='running'
        assert good()['item']['can_edit'] is True
        saved=library.load_document(folder)
        assert [s.text for s in saved.sections]==['Second edited','First edited','Tail edited']
        assert [s.shot.name if s.shot else None for s in saved.sections]==['shot-2.png',None,None]
        assert saved.sections[1].omitted.name=='shot-1.png'
        assert [s.notes for s in saved.sections]==[('second note',),('first note',),('tail note',)]
        shown=call('walkthroughs.detail',id=folder.name,spoken=True)
        assert shown['steps'][0]['text']=='Second edited' and shown['steps'][0]['image_path'].endswith('shot-2.png')
        assert json.loads((folder/'walkthrough.original.json').read_text())['sections'][0]['text']=='First'
        assert (folder/'walkthrough.pdf').read_bytes().startswith(b'%PDF')
        assert (folder/'walkthrough.docx').is_file()
        manifest=library.read_json(folder/'walkthrough.json')
        manifest['sections'][2]['text']='x'*500+' searchable-late-token'
        library.atomic_json(folder/'walkthrough.json',manifest)
        assert call('walkthroughs.list',query='searchable-late-token')['total']==1
    ''', tmp_path)


def test_retry_versions_edit_selected_earlier_export_and_copy_stay_consistent(tmp_path):
    run_native(r'''
        from meeting.agent.installed import core
        prompts=[]
        answers=iter(['# First write-up\n\nFirst prose.\n\n![First](shot-1.png)',
                      '# Newest write-up\n\nNewest prose.\n\n![Second](shot-2.png)'])
        def agent(*args,**kwargs):
            prompts.append(kwargs);return SimpleNamespace(ok=True,canceled=False,error='',text=next(answers))
        core.run_reading_task=agent
        available=call('walkthroughs.styles')['items']
        assert available[0]['id']=='walkthrough' and len(available)>=5
        call('walkthroughs.write_up.retry',id=folder.name,style_id='bug-report')
        good()
        assert 'bug report' in prompts[0]['user_prompt'].lower()
        assert 'first note' in prompts[0]['user_prompt'] and 'second note' in prompts[0]['user_prompt']
        call('walkthroughs.write_up.retry',id=folder.name,instructions='Custom retry instructions')
        good()
        assert 'Custom retry instructions' in prompts[1]['user_prompt']
        assert library.earlier_write_ups(folder)==[1]
        latest=(folder/'write-up.md').read_bytes()
        selected=call('walkthroughs.detail',id=folder.name,spoken=False,version=1)
        assert 'First write-up' in selected['markdown'] and selected['version']==1
        assert len(selected['versions'])==3
        call('walkthroughs.write_up.save',id=folder.name,version=1,markdown='# Earlier edited\n\n![First](shot-1.png)')
        operation=good()
        assert operation['item']['can_retry'] is True and operation['item']['version']==1
        assert (folder/'write-up.md').read_bytes()==latest
        exported=call('walkthroughs.export',id=folder.name,version=1,format='pdf')
        assert exported['source_path']==str(folder/'write-up-1.pdf')
        assert 'Earlier edited' in call('walkthroughs.export',id=folder.name,version=1,format='md')['text']
        assert 'First' in call('walkthroughs.detail',id=folder.name,spoken=True)['steps'][0]['text']
        call('walkthroughs.copy',id=folder.name,version=1,format='md')
        assert 'Earlier edited' in clipboard.copies[-1][1]
        assert str(folder/'shot-1.png').replace(chr(92),'/') in clipboard.copies[-1][1].replace(chr(92),'/')
        call('walkthroughs.screenshot.copy',id=folder.name,filename='shot-2.png')
        assert clipboard.copies[-1][0]==[folder/'shot-2.png'] and clipboard.copies[-1][2] is not None
        try:call('walkthroughs.detail',id=folder.name,version=99)
        except ValueError:pass
        else:raise AssertionError('unknown version accepted')
    ''', tmp_path)


def test_async_cancel_preserves_documents_and_keeps_lease_until_worker_settles(tmp_path):
    run_native(r'''
        from meeting.agent.installed import core
        write_up._write(library.load_result(folder,spoken=True),'# Original\n\n![One](shot-1.png)',document.Look())
        before={path.name:path.read_bytes() for path in folder.iterdir()}
        entered=threading.Event()
        def agent(*args,**kwargs):
            entered.set();assert kwargs['cancel_event'].wait(5)
            return SimpleNamespace(ok=False,canceled=True,error='',text='')
        core.run_reading_task=agent
        call('walkthroughs.write_up.retry',id=folder.name)
        assert entered.wait(3) and controller._walkthrough_library_busy
        assert controller.transcription_runtime._claim_job()
        try:call('walkthroughs.delete',id=folder.name)
        except ValueError:pass
        else:raise AssertionError('concurrent mutation accepted')
        call('walkthroughs.write_up.cancel',id=folder.name)
        assert settled()['status']=='canceled'
        assert not controller._walkthrough_library_busy
        assert controller.transcription_runtime.has_active_job
        assert not controller.transcription_runtime._cancel_requested.is_set()
        controller.transcription_runtime._finish_job()
        assert before=={path.name:path.read_bytes() for path in folder.iterdir()}
        render=document.write_markdown_pdf
        started,release=threading.Event(),threading.Event()
        def slow_render(*args,**kwargs):
            started.set();assert release.wait(5);return render(*args,**kwargs)
        document.write_markdown_pdf=slow_render
        call('walkthroughs.write_up.save',id=folder.name,markdown='# Must not publish')
        assert started.wait(3)
        call('walkthroughs.write_up.cancel',id=folder.name)
        release.set()
        assert settled()['status']=='canceled'
        assert before=={path.name:path.read_bytes() for path in folder.iterdir()}
        assert any(event=='walkthroughs.busy' and data['busy'] for event,data in events)
        assert any(event=='walkthroughs.busy' and not data['busy'] for event,data in events)
    ''', tmp_path)


def test_real_recovery_claims_transcription_job_and_preserves_draft_on_cancel(tmp_path):
    run_native(r'''
        draft_folder=context.captures_root/'2026-11-02_10-00-00'
        draft_folder.mkdir()
        interrupted=WalkthroughSession(started_at=datetime(2026,11,2,10),make_folder=lambda _:draft_folder)
        interrupted.add_shot(Image.new('RGB',(40,30),'green'),1.)
        interrupted.hold_note(.2);interrupted.release_note(.8)
        interrupted.checkpoint_recording('a'*32)
        interrupted.saved_shots()
        audio=draft_folder/'audio.wav'
        with wave.open(str(audio),'wb') as stream:
            stream.setnchannels(1);stream.setsampwidth(2);stream.setframerate(16000)
            stream.writeframes(b'\0 '*48000)
        interrupted.preserve_audio(audio)
        writer=interrupted._writer
        interrupted.end()
        if writer is not None: writer.shutdown(wait=True)
        original={p.name:p.read_bytes() for p in draft_folder.iterdir()}
        shown=call('walkthroughs.detail',id=draft_folder.name)
        assert shown['is_draft'] and shown['can_recover'] and len(shown['steps'])==1
        assert shown['notes_s']==[[.2,.8]]
        backend.release=threading.Event()
        call('walkthroughs.recover',id=draft_folder.name)
        assert backend.entered.wait(3)
        assert controller.transcription_runtime._active_job.mode=='walkthrough'
        call('walkthroughs.write_up.cancel',id=draft_folder.name)
        assert settled(draft_folder.name)['status']=='canceled'
        after={p.name:p.read_bytes() for p in draft_folder.iterdir()}
        assert original==after,{name:(original.get(name),after.get(name)) for name in original.keys()|after.keys() if original.get(name)!=after.get(name)}
        assert not controller.transcription_runtime.has_active_job
        backend.release=None;backend.entered.clear();backend.reset_cancel_flag()
        call('walkthroughs.recover',id=draft_folder.name)
        recovered=good(draft_folder.name)
        assert not recovered['item']['is_draft']
        assert (draft_folder/'walkthrough.pdf').read_bytes().startswith(b'%PDF')
        assert not (draft_folder/'draft.json').exists()
        assert any(section.notes for section in library.load_document(draft_folder).sections)
        assert controller.transcription_runtime._claim_job()
        assert not call('walkthroughs.write_up.cancel',id=draft_folder.name)['running']
        assert controller.transcription_runtime.has_active_job
        assert not controller.transcription_runtime._cancel_requested.is_set()
        controller.transcription_runtime._finish_job()
    ''', tmp_path)


def test_retention_confirmation_exempts_drafts_and_corrupt_rows_remain_selectable(tmp_path):
    run_native(r'''
        import os
        old=time.time()-86400*5
        os.utime(folder/'walkthrough.json',(old,old))
        draft=context.captures_root/'2026-11-03_10-00-00'
        draft.mkdir();(draft/'draft.json').write_text(json.dumps({'version':1,'started_at':'2026-11-03T10:00:00','shots':[],'notes_s':[]}))
        broken=context.captures_root/'2026-11-04_10-00-00'
        broken.mkdir();(broken/'walkthrough.json').write_text('{broken')
        rows=call('walkthroughs.list')
        assert len(rows['items'])==3 and rows['warnings']
        item=call('walkthroughs.detail',id=broken.name)
        assert item['status']=='needs_recovery' and item['error'] and not item['can_recover']
        preview=call('walkthroughs.retention',days=1)
        assert preview['requires_confirmation'] and preview['expired_ids']==[folder.name]
        assert library.retention_days(context.captures_root)==0
        call('walkthroughs.retention',days=1,confirmed=True)
        good('retention')
        assert not folder.exists() and draft.exists() and broken.exists()
        assert call('walkthroughs.retention')['days']==1
        (broken/'unrelated.txt').write_text('keep')
        call('walkthroughs.delete',id=broken.name)
        assert settled(broken.name)['status']=='failed'
        assert (broken/'walkthrough.json').read_text()=='{broken' and (broken/'unrelated.txt').read_text()=='keep'
        assert not controller._walkthrough_library_busy
    ''', tmp_path)


def test_final_operation_items_are_emitted_once_and_idle_snapshot_keeps_bounded_metadata(tmp_path):
    run_native(r'''
        from services.native_desktop.commands.walkthroughs import _publish_operation
        long_item={'id':'large-document','markdown':'LONG_DOCUMENT_CONTENT'*100_000,
                   'steps':[{'text':'LONG_DOCUMENT_CONTENT'*100_000}]}
        active={'id':'active-job','job_id':'active','kind':'write_up.retry','status':'running','cancelable':True}
        _publish_operation(context,active)
        for number in range(100):
            _publish_operation(context,{'id':f'document-{number}','job_id':f'job-{number}',
                'kind':'write_up.save','status':'done','error':'','cancelable':True,'item':long_item})
        operations=ui.state['walkthrough_operations']
        assert len(operations)==65 and operations['active-job']['status']=='running'
        assert 'document-0' not in operations and 'document-99' in operations
        assert all('item' not in metadata and 'result' not in metadata for metadata in operations.values())
        finals=[data for event,data in events if event=='walkthroughs.operation' and data.get('status')=='done']
        assert len(finals)==100 and all(data['item'] is long_item for data in finals)
        snapshot=json.dumps(context.snapshot(),ensure_ascii=True)
        assert len(snapshot)<32_768 and 'LONG_DOCUMENT_CONTENT' not in snapshot
    ''', tmp_path)
