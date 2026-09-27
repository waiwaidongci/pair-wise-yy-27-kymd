# 数字人文文本校勘

这是一个 Python 标准库实现的校勘工作台，使用 SQLite 保存作品、版本、残片、转录、段落、异文、注释、修订层和快照，并通过 `http.server` 暴露 JSON API。

## 启动与测试

```bash
python app.py
python -m unittest discover -s tests -v
```

默认端口 `8114`，地址 <http://127.0.0.1:8114>。首次启动创建一个带缺页残片、不可辨标记和一条待重校散页回补登记的示例。数据库可通过 `COLLATION_DB` 指定，端口可通过 `PORT` 指定。

## 业务规则

- 版本类型限定为 `version`、`fragment`、`transcription`。
- 段落和版本必须属于同一作品，同一版本不能重复对齐同一段落。
- 只有负责人或被单独授权的编辑可以修改对应版本；其他用户只有查看权限。
- `[缺页]`、`[不可辨]`、`[残损]` 等标记会参与校勘稿导出和缺口统计，不匹配的方括号会拒绝保存。
- 每次新增或修改异文都会产生递增修订号和 JSON 快照；提交必须携带 `expected_revision`，旧页面不能覆盖新层。
- 锁定段落由负责人执行，锁定后任何新修订都会被拒绝。
- 散页回补需登记版本、叶序、影像编号和释文；同一版本同一叶序已有记录时拒绝。
- 释文与现有对齐重叠时该叶留在待重校，负责人复核通过后才替换该叶；原对齐和旧异文结论留档，留档异文不能再修订，待重校期间该版本不能新增异文。
- 含待重校内容的段落不能生成定本，也不能锁定定稿；定本接口和页面会列出叶序、来源和阻挡原因。

## 主要接口

- `POST /api/users`、`POST /api/works`
- `POST /api/works/{id}/witnesses`、`POST /api/witnesses/{id}/editors`
- `POST /api/works/{id}/passages`、`POST /api/works/{id}/access`
- `POST /api/alignments`
- `POST /api/variants`、`POST /api/variants/{id}/revisions`
- `GET /api/passages/{id}/snapshots/{revision}?user_id=...`
- `POST /api/passages/{id}/lock`
- `POST /api/leaves`、`POST /api/leaves/{id}/review`
- `GET /api/works/{id}/collation?user_id=...`
- `GET /api/works/{id}/definitive?user_id=...`

导出接口把版本对齐、异文、注释、残损缺口和锁定状态组合成可复核的校勘稿；定本接口按段落给出定本正文或阻挡信息。
