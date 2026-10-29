"""Small deterministic tests for the optional project layer."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[2]

class SpaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.work=Path(self.tmp.name)
        self.db=self.work/'ac.db'
        for a in ['codex','antigravity','other']:
            self.run_cmd('ac.py','register',a)

    def run_cmd(self,script,*args,expected=0):
        r=subprocess.run([sys.executable, str(ROOT/script),'--db',str(self.db),*map(str,args)],
                         cwd=self.work,text=True,capture_output=True,encoding='utf-8',timeout=8)
        self.assertEqual(r.returncode,expected,r.stdout+r.stderr)
        return r.stdout.strip()

    def file(self,name,data):
        path=self.work/name
        path.write_text(json.dumps(data,ensure_ascii=False),encoding='utf-8')
        return str(path)

    def test_memory_authority_delta_and_catalog(self):
        a=self.file('role1.json',{'agent':'antigravity','role':'draw'})
        b=self.file('role2.json',{'agent':'codex','role':'draw'})
        self.run_cmd('ac_space.py','record','--kind','role','--key','image','--agent','codex',
                     '--file',a,'--authority','user_confirmed','--evidence','user:session123')
        self.run_cmd('ac_space.py','record','--kind','role','--key','image','--agent','other',
                     '--file',b,'--authority','agent_proposal')
        current=json.loads(self.run_cmd('ac_space.py','context','--kind','role'))
        self.assertEqual(current[0]['value']['agent'],'antigravity')
        changes=json.loads(self.run_cmd('ac_space.py','context','--since','1'))
        self.assertEqual(changes[0]['revision'],2)
        self.assertEqual(changes[0]['authority'],'agent_proposal')
        self.run_cmd('ac_space.py','record','--kind','setting','--key','ratio','--agent','codex',
                     '--file',a,'--authority','user_confirmed',expected=4)
        self.assertIn('acs_jobs',json.loads(self.run_cmd('ac_space.py','discover'))['tables'])

    def test_board_dependencies_and_parallel_claim_safety(self):
        p1=self.file('task1.json',{'id':'T1','title':'做A','requester':'codex','input':{},'output':{},'capabilities':[]})
        p2=self.file('task2.json',{'id':'T2','title':'做B','requester':'codex','depends_on':['T1'],'parent_id':'T1'})
        self.run_cmd('ac_space.py','post','--file',p1)
        self.run_cmd('ac_space.py','post','--file',p2)
        board=json.loads(self.run_cmd('ac_space.py','board'))
        self.assertEqual([x['status'] for x in board],['open','blocked'])
        claim=self.run_cmd('ac_space.py','claim','T1','--agent','antigravity').split()        self.assertEqual(claim[:2],['CLAIMED','T1'])
        token=claim[2]
        self.run_cmd('ac_space.py','claim','T1','--agent','other',expected=6)
        out=self.work/'hello.md';out.write_text('hello',encoding='utf-8')
        self.run_cmd('ac_space.py','submit','T1','--agent','antigravity','--token',token,'--artifact','hello.md')
        self.run_cmd('ac_space.py','accept','T1','--agent','other',expected=4)
        opened=self.run_cmd('ac_space.py','accept','T1','--agent','codex')
        self.assertIn('T2',opened)
        self.assertEqual(json.loads(self.run_cmd('ac_space.py','job','T2'))['status'],'open')
        self.assertEqual(json.loads(self.run_cmd('ac_space.py','job','T1'))['sha256'],__import__('hashlib').sha256(b'hello').hexdigest())

    def test_unknown_agent_and_bad_dependency(self):
        p=self.file('x.json',{'id':'T3','title':'thing','requester':'codex','depends_on':['MISSING']})
        self.run_cmd('ac_space.py','post','--file',p,expected=4)
        self.run_cmd('ac_space.py','claim','MISSING','--agent','codex',expected=4)
        p=self.file('y.json',{'id':'T4','title':'thing','requester':'missing'})
        self.run_cmd('ac_space.py','post','--file',p,expected=4)

if __name__=='__main__':unittest.main()
