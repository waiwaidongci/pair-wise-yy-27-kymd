import os, sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from database import CollationDB, DomainError

class CollationFlowTest(unittest.TestCase):
    def setUp(self):
        fd,self.path=tempfile.mkstemp(suffix=".db"); os.close(fd); self.db=CollationDB(self.path)
        self.owner=self.db.add_user("负责人","owner"); self.editor=self.db.add_user("编辑","editor"); self.reviewer=self.db.add_user("审阅","reviewer"); self.outsider=self.db.add_user("外部","reviewer")
        self.work=self.db.create_work("残卷","异文比较",self.owner)
        self.w1=self.db.add_witness(self.work,"甲本","version"); self.w2=self.db.add_witness(self.work,"乙本","fragment","馆藏残片","中段缺页")
        self.db.grant_witness_editor(self.w2,self.editor,self.owner); self.db.grant_work_access(self.work,self.reviewer,"view",self.owner)
        self.passage=self.db.add_passage(self.work,"第一节","春水东流，故人南去。",self.owner)
        self.db.align_passage(self.passage,self.w1,"春水东流，故人南去。",1,self.owner)
        self.db.align_passage(self.passage,self.w2,"春水东流，[缺页]",2,self.editor)
    def tearDown(self): self.db.close(); os.unlink(self.path)
    def test_multilayer_revision_snapshot_export_and_lock(self):
        variant=self.db.create_variant(self.passage,self.w2,"春水东流，故人南去。","按语义补足",self.editor,0)
        rev=self.db.update_variant(variant,"春水东流，[不可辨]人南去。","墨迹受损，不再直接补写",self.editor,1)
        self.assertEqual(2,rev)
        snap=self.db.get_snapshot(self.passage,2,self.owner)
        self.assertEqual(2,snap["layer"])
        exported=self.db.export_collation(self.work,self.reviewer)
        self.assertEqual(1,exported["gap_count"])
        self.assertTrue(exported["passages"][0]["variants"][0]["notes"] == [])
        self.db.lock_passage(self.passage,self.owner,"定稿")
        with self.assertRaisesRegex(DomainError,"锁定"):
            self.db.update_variant(variant,"另一文本","无意义修改",self.editor,2)
    def test_optimistic_lock_permission_and_mark_validation(self):
        first=self.db.create_variant(self.passage,self.w2,"补足一","理由一",self.editor,0)
        with self.assertRaisesRegex(DomainError,"版本冲突"):
            self.db.create_variant(self.passage,self.w2,"补足二","理由二",self.editor,0)
        with self.assertRaisesRegex(DomainError,"无权"):
            self.db.create_variant(self.passage,self.w2,"补足三","理由三",self.reviewer,1)
        with self.assertRaisesRegex(DomainError,"无权"):
            self.db.export_collation(self.work,self.outsider)
        with self.assertRaisesRegex(DomainError,"括号"):
            self.db.align_passage(self.passage,self.w1,"文本[未闭合",9,self.owner)
    def test_recovered_leaf_reintegration(self):
        variant=self.db.create_variant(self.passage,self.w2,"春水东流，故人南去。","依文意拟补",self.editor,0)
        leaf=self.db.register_leaf(self.w2,"叶三","IMG-003","故人南去。",self.editor)
        with self.assertRaisesRegex(DomainError,"同版本同叶"):
            self.db.register_leaf(self.w2,"叶三","IMG-004","他文。",self.editor)
        with self.assertRaisesRegex(DomainError,"无权"):
            self.db.register_leaf(self.w2,"叶四","IMG-005","故人。",self.reviewer)
        blocked=self.db.passage_definitive(self.passage,self.owner)
        self.assertEqual("blocked",blocked["status"])
        self.assertEqual("叶三",blocked["blocks"][0]["leaf_seq"])
        self.assertIn("乙本",blocked["blocks"][0]["source"])
        self.assertIn("待负责人复核",blocked["blocks"][0]["reason"])
        with self.assertRaisesRegex(DomainError,"负责人"):
            self.db.review_leaf(leaf,self.editor,True)
        self.assertEqual("approved",self.db.review_leaf(leaf,self.owner,True))
        exported=self.db.export_collation(self.work,self.owner)
        alignment=[a for a in exported["passages"][0]["alignments"] if a["witness_id"]==self.w2][0]
        self.assertEqual("春水东流，故人南去。",alignment["aligned_text"])
        self.assertEqual([],exported["passages"][0]["variants"])
        archived=self.db.conn.execute("SELECT * FROM alignment_archive WHERE leaf_id=?",(leaf,)).fetchall()
        self.assertEqual(1,len(archived))
        self.assertIn("[缺页]",archived[0]["aligned_text"])
        self.assertIn("依文意拟补",archived[0]["variants_json"])
        self.assertEqual("ready",self.db.passage_definitive(self.passage,self.owner)["status"])
        leaf2=self.db.register_leaf(self.w2,"叶四","IMG-006","春水东流，故人南去。",self.editor)
        leaves={l["id"]:l for l in self.db.list_leaves(self.work,self.owner)}
        self.assertEqual("recollation",leaves[leaf2]["status"])
        blocked=self.db.passage_definitive(self.passage,self.owner)
        self.assertEqual("blocked",blocked["status"])
        self.assertIn("待重校",blocked["blocks"][0]["reason"])
        self.db.review_leaf(leaf2,self.owner,True)
        self.assertEqual("ready",self.db.passage_definitive(self.passage,self.owner)["status"])

if __name__=="__main__": unittest.main()
