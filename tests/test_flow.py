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
    def test_recovered_leaf_review_replaces_and_archives(self):
        variant=self.db.create_variant(self.passage,self.w2,"春水东流，故人南去。","依据缺页拟补",self.editor,0)
        leaf=self.db.register_recovered_leaf(self.w2,self.passage,"叶三","IMG-003","春水东流，故人南去。",self.editor)
        with self.assertRaisesRegex(DomainError,"已有回补登记"):
            self.db.register_recovered_leaf(self.w2,self.passage,"叶三","IMG-004","他句",self.editor)
        with self.assertRaisesRegex(DomainError,"暂不能新增异文"):
            self.db.create_variant(self.passage,self.w2,"再补","继续拟补",self.editor,1)
        definitive=self.db.export_definitive(self.work,self.owner)
        self.assertFalse(definitive["can_generate"])
        blocker=definitive["passages"][0]["blockers"][0]
        self.assertEqual("叶三",blocker["leaf_no"]); self.assertIn("乙本",blocker["source"]); self.assertIn("待重校",blocker["reason"])
        self.assertIsNone(definitive["passages"][0]["definitive_text"])
        with self.assertRaisesRegex(DomainError,"不能生成定本"):
            self.db.lock_passage(self.passage,self.owner,"定稿")
        with self.assertRaisesRegex(DomainError,"负责人"):
            self.db.review_recovered_leaf(leaf,self.editor)
        self.assertEqual("applied",self.db.review_recovered_leaf(leaf,self.owner))
        exported=self.db.export_collation(self.work,self.owner)
        passage=exported["passages"][0]
        self.assertEqual("春水东流，故人南去。",passage["alignments"][1]["aligned_text"])
        self.assertEqual(0,exported["gap_count"])
        self.assertEqual([],passage["variants"])
        self.assertEqual(1,len(passage["archived_variants"]))
        archives=self.db.snapshot()["alignment_archives"]
        self.assertEqual("春水东流，[缺页]",archives[0]["aligned_text"])
        self.assertEqual(variant,archives[0]["variants"][0]["id"])
        with self.assertRaisesRegex(DomainError,"留档"):
            self.db.update_variant(variant,"再改","试图改旧结论",self.editor,1)
        definitive=self.db.export_definitive(self.work,self.owner)
        self.assertTrue(definitive["can_generate"])
        self.assertEqual("春水东流，故人南去。",definitive["passages"][0]["definitive_text"])
    def test_recovered_leaf_overlap_status_and_permission(self):
        with self.assertRaisesRegex(DomainError,"无权"):
            self.db.register_recovered_leaf(self.w2,self.passage,"叶四","IMG-005","春水东流，故人南去。",self.outsider)
        leaf=self.db.register_recovered_leaf(self.w2,self.passage,"叶四","IMG-005","春水东流，故人南去。",self.editor)
        row=[l for l in self.db.snapshot()["recovered_leaves"] if l["id"]==leaf][0]
        self.assertEqual("pending_recollate",row["status"]); self.assertEqual("春水东流，",row["overlap_text"])
        w3=self.db.add_witness(self.work,"丙本","version")
        leaf2=self.db.register_recovered_leaf(w3,self.passage,"叶一","IMG-009","全新一句。",self.owner)
        row2=[l for l in self.db.snapshot()["recovered_leaves"] if l["id"]==leaf2][0]
        self.assertEqual("pending_review",row2["status"])
        blockers=self.db.export_definitive(self.work,self.owner)["passages"][0]["blockers"]
        self.assertEqual("散页回补登记待负责人复核",blockers[1]["reason"])
        self.assertEqual("applied",self.db.review_recovered_leaf(leaf2,self.owner))
        alignments=self.db.export_collation(self.work,self.owner)["passages"][0]["alignments"]
        self.assertEqual("全新一句。",[a for a in alignments if a["witness_id"]==w3][0]["aligned_text"])

if __name__=="__main__": unittest.main()
