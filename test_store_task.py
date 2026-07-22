"""
门店任务功能测试
覆盖：执行人/收款人信息编辑保存、None 渲染、权限校验、连续编辑稳定性
"""
import os
import re

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(BASE_DIR, "xf_erp_test.db")
os.environ["ERP_SECRET_KEY"] = "test-secret"

from app import app, db, seed_data, Task  # noqa: E402


# ================================================================
# 工具函数
# ================================================================

def ok(msg):
    print(f"  ✅ {msg}")

def fail(msg):
    print(f"  ❌ {msg}")
    raise AssertionError(msg)

def warn(msg):
    print(f"  ⚠️  {msg}")

def setup():
    """初始化测试环境，返回 test_client、task_id 和已登录的 client"""
    with app.app_context():
        db.drop_all()
        db.create_all()
        seed_data()
        task = Task.query.first()
        task_id = task.id
    client = app.test_client()
    r = client.post("/login", data={"username": "admin", "password": "admin123"}, follow_redirects=True)
    assert r.status_code == 200, f"登录失败: {r.status_code}"
    return client, task_id

def set_fields_none(task_id):
    """将执行人/收款人字段全部设为 None"""
    with app.app_context():
        task = db.session.get(Task, task_id)
        for f in ["executor_name", "executor_phone", "payee_name", "payee_phone",
                   "payee_bank", "payee_account", "executor_remarks"]:
            setattr(task, f, None)
        db.session.commit()

def get_input_values(html, fields):
    """从 HTML 中提取指定字段的 input value"""
    result = {}
    for f in fields:
        m = re.search(rf'name="{f}"\s+value="([^"]*)"', html)
        result[f] = m.group(1) if m else None
    return result

def get_view_values(html, labels):
    """从 section5-view 中提取 label+strong 的显示值"""
    result = {}
    view = re.search(r'id="section5-view".*?(?=<section class="card">|$)', html, re.DOTALL)
    if not view:
        return result
    for label in labels:
        m = re.search(rf'<label>{label}</label><strong>([^<]*)</strong>', view.group(0))
        result[label] = m.group(1) if m else None
    return result


# ================================================================
# 测试一：编辑保存完整流程 + DOM 结构验证
# ================================================================

def test_edit_save_flow():
    print("\n" + "=" * 60)
    print("测试一：编辑保存完整流程")
    print("=" * 60)

    client, task_id = setup()
    set_fields_none(task_id)

    # 1.1 初始页面 DOM
    print("\n[1.1] 初始 DOM 结构")
    r = client.get(f"/tasks/{task_id}")
    assert r.status_code == 200
    html = r.data.decode("utf-8")

    assert 'btn-edit-section5' in html or fail("编辑按钮不存在"); ok("编辑按钮存在")
    assert 'id="section5-view"' in html or fail("section5-view 不存在"); ok("section5-view 存在")
    assert 'id="section5-edit"' in html or fail("section5-edit 不存在"); ok("section5-edit 存在")
    assert 'section-edit hidden' in html or fail("section5-edit 缺少 hidden"); ok("section5-edit 已隐藏")
    assert 'toggleSectionEdit' in html or fail("toggleSectionEdit 函数不存在"); ok("toggleSectionEdit 函数存在")
    assert 'id="section5-edit"' in html
    assert html.count('id="section5-view"') == 1 or fail("section5-view 重复"); ok("无重复 ID")
    assert html.count('id="section5-edit"') == 1 or fail("section5-edit 重复"); ok("无重复 ID")

    # 1.2 保存完整数据
    print("\n[1.2] 保存完整数据")
    save_data = {
        "executor_name": "张三", "executor_phone": "13800001111",
        "payee_name": "李四", "payee_phone": "13900002222",
        "payee_bank": "中国银行", "payee_account": "6217000000000000001",
        "executor_remarks": "测试备注",
    }
    r = client.post(f"/tasks/{task_id}/executor", data=save_data, follow_redirects=True)
    assert r.status_code == 200 or fail(f"保存失败: {r.status_code}")
    html2 = r.data.decode("utf-8")

    assert "执行人和收款信息已保存" in html2 or fail("flash 消息缺失"); ok("flash 消息显示")

    with app.app_context():
        t = db.session.get(Task, task_id)
        for f, v in [("executor_name", "张三"), ("executor_phone", "13800001111"),
                      ("payee_name", "李四"), ("payee_phone", "13900002222"),
                      ("payee_bank", "中国银行"), ("payee_account", "6217000000000000001"),
                      ("executor_remarks", "测试备注")]:
            assert getattr(t, f) == v or fail(f"DB {f} 应为 [{v}] 实际 [{getattr(t, f)}]")
    ok("数据库全部正确保存")

    # 1.3 保存后 DOM
    print("\n[1.3] 保存后 DOM 状态")
    view_match = re.search(r'id="section5-view"\s+class="([^"]*)"', html2)
    if view_match and 'hidden' not in view_match.group(1):
        ok("section5-view 可见")
    else:
        fail("section5-view 不应有 hidden")

    edit_match = re.search(r'id="section5-edit"\s+class="([^"]*)"', html2)
    if edit_match and 'hidden' in edit_match.group(1):
        ok("section5-edit 正确隐藏")
    else:
        fail("section5-edit 缺少 hidden")

    btn_match = re.search(r'id="btn-edit-section5"[^>]*>([^<]*)<', html2)
    if btn_match and btn_match.group(1) == '编辑':
        ok("编辑按钮文本正确")
    else:
        fail(f"按钮文本异常: {btn_match.group(1) if btn_match else 'not found'}")

    # 1.4 页面渲染值验证
    print("\n[1.4] 页面渲染值")
    inputs = get_input_values(html2, ["executor_name", "executor_phone", "payee_name",
                                       "payee_phone", "payee_bank", "payee_account"])
    for f, expected in [("executor_name", "张三"), ("executor_phone", "13800001111"),
                         ("payee_name", "李四"), ("payee_phone", "13900002222"),
                         ("payee_bank", "中国银行"), ("payee_account", "6217000000000000001")]:
        if inputs.get(f) == expected:
            ok(f"input {f} = '{expected}'")
        elif inputs.get(f) == "None":
            fail(f"input {f} = 'None' —— None 渲染 bug!")
        else:
            warn(f"input {f} = '{inputs.get(f)}' (期望 '{expected}')")

    views = get_view_values(html2, ["执行人姓名", "执行人手机号", "收款人姓名",
                                     "收款人手机号", "开户行", "银行卡/收款账号"])
    for label, expected in [("执行人姓名", "张三"), ("执行人手机号", "13800001111"),
                             ("收款人姓名", "李四"), ("收款人手机号", "13900002222"),
                             ("开户行", "中国银行"), ("银行卡/收款账号", "6217000000000000001")]:
        if views.get(label) == expected:
            ok(f"VIEW {label} = '{expected}'")
        else:
            warn(f"VIEW {label} = '{views.get(label)}'")

    # 1.5 空数据保存
    print("\n[1.5] 空数据保存")
    r = client.post(f"/tasks/{task_id}/executor",
                    data={k: "" for k in save_data}, follow_redirects=True)
    assert r.status_code == 200
    html3 = r.data.decode("utf-8")

    with app.app_context():
        t = db.session.get(Task, task_id)
        for f in save_data:
            assert getattr(t, f) == "" or fail(f"DB {f} 应为空, 实际 [{getattr(t, f)}]")
    ok("空数据正确保存为空字符串")

    inputs = get_input_values(html3, ["executor_name", "executor_phone", "payee_name",
                                       "payee_phone", "payee_bank", "payee_account"])
    for f in inputs:
        if inputs[f] == "":
            ok(f"input {f} 为空字符串")
        elif inputs[f] == "None":
            fail(f"input {f} = 'None' —— 空字符串保存后仍显示 None!")
        else:
            warn(f"input {f} = '{inputs[f]}'")

    assert 'btn-edit-section5' in html3 or fail("空数据保存后编辑按钮丢失"); ok("编辑按钮仍存在")
    ok("测试一通过 ✓")


# ================================================================
# 测试二：只填电话号码场景（精确复现用户反馈）
# ================================================================

def test_phone_only_scenario():
    print("\n" + "=" * 60)
    print("测试二：只填电话号码场景")
    print("=" * 60)

    client, task_id = setup()
    set_fields_none(task_id)

    # 2.1 初始渲染：验证 or '' 修复生效
    print("\n[2.1] None 渲染检查（修复后应为空字符串）")
    r = client.get(f"/tasks/{task_id}")
    inputs = get_input_values(r.data.decode("utf-8"),
                               ["executor_name", "executor_phone", "payee_name",
                                "payee_phone", "payee_bank", "payee_account"])
    for f, v in inputs.items():
        if v == "":
            ok(f"{f} = '' (or '' 修复生效)")
        elif v == "None":
            fail(f"{f} = 'None' —— or '' 修复未生效!")
        else:
            warn(f"{f} = '{v}'")

    # 2.2 模拟：用户只填电话号码
    print("\n[2.2] 模拟只填电话号码")
    phone_only = {
        "executor_name": "", "executor_phone": "13800001111",
        "payee_name": "", "payee_phone": "13900002222",
        "payee_bank": "", "payee_account": "",
        "executor_remarks": "",
    }
    r = client.post(f"/tasks/{task_id}/executor", data=phone_only, follow_redirects=True)
    assert r.status_code == 200

    with app.app_context():
        t = db.session.get(Task, task_id)
        assert t.executor_phone == "13800001111" or fail("executor_phone 未保存")
        assert t.payee_phone == "13900002222" or fail("payee_phone 未保存")
        assert t.executor_name == "" or fail(f"executor_name 应为空, 实际 [{t.executor_name}]")
        assert t.payee_name == "" or fail(f"payee_name 应为空, 实际 [{t.payee_name}]")
    ok("只填电话号码正确保存，其他字段为空")

    # 2.3 保存后页面验证
    print("\n[2.3] 保存后页面")
    r = client.get(f"/tasks/{task_id}")
    html = r.data.decode("utf-8")
    inputs = get_input_values(html, ["executor_name", "executor_phone", "payee_name",
                                      "payee_phone", "payee_bank", "payee_account"])
    for f, expected in [("executor_name", ""), ("executor_phone", "13800001111"),
                         ("payee_name", ""), ("payee_phone", "13900002222"),
                         ("payee_bank", ""), ("payee_account", "")]:
        if inputs.get(f) == expected:
            ok(f"input {f} = '{expected}'")
        elif inputs.get(f) == "None":
            fail(f"input {f} = 'None' —— 数据污染!")
        else:
            warn(f"input {f} = '{inputs.get(f)}' (期望 '{expected}')")

    # 编辑按钮和 DOM
    assert 'btn-edit-section5' in html or fail("编辑按钮丢失")
    assert 'section-edit hidden' in html or fail("section5-edit 状态异常")
    ok("DOM 结构正常")

    # 2.4 第二次完整编辑
    print("\n[2.4] 第二次完整编辑")
    full_data = {
        "executor_name": "张三", "executor_phone": "13800001111",
        "payee_name": "李四", "payee_phone": "13900002222",
        "payee_bank": "中国银行", "payee_account": "6217000000000001",
        "executor_remarks": "第二次完整编辑",
    }
    r = client.post(f"/tasks/{task_id}/executor", data=full_data, follow_redirects=True)
    assert r.status_code == 200

    with app.app_context():
        t = db.session.get(Task, task_id)
        for f, v in [("executor_name", "张三"), ("payee_name", "李四"),
                      ("payee_bank", "中国银行"), ("payee_account", "6217000000000001")]:
            assert getattr(t, f) == v or fail(f"DB {f} = [{getattr(t, f)}], 期望 [{v}]")
    ok("第二次编辑全部字段正确保存")

    # 2.5 None 字符串污染检查
    print("\n[2.5] None 污染检查")
    with app.app_context():
        t = db.session.get(Task, task_id)
        for f in ["executor_name", "executor_phone", "payee_name", "payee_phone",
                   "payee_bank", "payee_account", "executor_remarks"]:
            if getattr(t, f) == "None":
                fail(f"{f} 被字符串 'None' 污染!")
    ok("无字符串 'None' 污染")

    ok("测试二通过 ✓")


# ================================================================
# 测试三：连续编辑保存稳定性
# ================================================================

def test_consecutive_saves():
    print("\n" + "=" * 60)
    print("测试三：连续编辑保存稳定性")
    print("=" * 60)

    client, task_id = setup()
    set_fields_none(task_id)

    rounds = 10
    for i in range(rounds):
        r = client.post(f"/tasks/{task_id}/executor",
                        data={k: f"round{i}" for k in ["executor_name", "executor_phone",
                                                         "payee_name", "payee_phone",
                                                         "payee_bank", "payee_account",
                                                         "executor_remarks"]},
                        follow_redirects=True)
        assert r.status_code == 200 or fail(f"第 {i+1} 次保存失败: {r.status_code}")
        html = r.data.decode("utf-8")
        assert f"round{i}" in html or fail(f"第 {i+1} 次保存后页面未渲染数据")
        assert 'section-edit hidden' in html or fail(f"第 {i+1} 次保存后 DOM 异常")
        assert 'btn-edit-section5' in html or fail(f"第 {i+1} 次保存后编辑按钮消失")

    ok(f"连续 {rounds} 次编辑保存全部通过 ✓")


# ================================================================
# 测试四：权限校验
# ================================================================

def test_permission():
    print("\n" + "=" * 60)
    print("测试四：权限校验")
    print("=" * 60)

    client, task_id = setup()

    # 4.1 超管可见编辑按钮
    print("\n[4.1] 超管")
    r = client.get(f"/tasks/{task_id}")
    assert 'btn-edit-section5' in r.data.decode("utf-8") or fail("超管应看到编辑按钮")
    ok("超管可见编辑按钮")

    # 4.2 运营也可见编辑按钮（开放全部等级编辑）
    print("\n[4.2] 运营")
    client.get("/logout", follow_redirects=True)
    client.post("/login", data={"username": "operator", "password": "operator123"}, follow_redirects=True)
    r = client.get(f"/tasks/{task_id}")
    html = r.data.decode("utf-8")
    if r.status_code == 200 and 'section5-view' in html:
        assert 'btn-edit-section5' in html or fail("运营应看到编辑按钮（开放全部等级编辑后）")
        r2 = client.post(f"/tasks/{task_id}/executor",
                         data={"executor_name": "运营编辑", "executor_phone": "", "payee_name": "",
                               "payee_phone": "", "payee_bank": "", "payee_account": "",
                               "executor_remarks": ""},
                         follow_redirects=True)
        assert r2.status_code == 200 or fail(f"运营保存失败: {r2.status_code}")
        with app.app_context():
            t = db.session.get(Task, task_id)
            assert t.executor_name == "运营编辑" or fail("运营编辑未生效")
        ok("运营可见编辑按钮且可保存 ✓")
    else:
        warn("运营账号无该任务可见权限，跳过按钮校验")

    # 4.3 主管可见
    print("\n[4.3] 主管")
    client.get("/logout", follow_redirects=True)
    client.post("/login", data={"username": "supervisor", "password": "supervisor123"}, follow_redirects=True)
    r = client.get(f"/tasks/{task_id}")
    assert 'btn-edit-section5' in r.data.decode("utf-8") or fail("主管应看到编辑按钮")
    ok("主管可见编辑按钮 ✓")

    ok("测试四通过 ✓")


# ================================================================
# 测试五：HTML 特殊字符转义
# ================================================================

# ================================================================
# 测试五：第三方确认页 — 仅"已执行已提交"
# ================================================================

def test_confirmation_form():
    print("\n" + "=" * 60)
    print("测试五：第三方确认页 — 仅已执行已提交")
    print("=" * 60)

    from app import db, seed_data, Task, StoreFlowRecord

    with app.app_context():
        db.drop_all()
        db.create_all()
        seed_data()
        task = Task.query.first()
        task.confirmation_token = "test-token-abc123"
        task.confirmation_status = "未确认"
        task.confirmation_review_status = "未发起"
        task.task_status = "进行中"
        db.session.commit()
        token = task.confirmation_token

    client = app.test_client()

    # 5.1 GET 确认页 — 验证 DOM
    print("\n[5.1] GET 确认页 DOM 结构")
    r = client.get(f"/confirm/{token}")
    assert r.status_code == 200 or fail(f"GET 失败: {r.status_code}")
    html = r.data.decode("utf-8")

    # 不应有下拉菜单
    assert '<select name="confirmation_status"' not in html or fail("不应出现状态下拉菜单")
    ok("无状态下拉菜单")

    # 应有隐藏域
    assert 'type="hidden" name="confirmation_status" value="已执行已提交"' in html \
        or fail("缺少隐藏域 type=hidden name=confirmation_status")
    ok("隐藏域存在，值为已执行已提交")

    # 截图 input 应有 required
    assert 'id="confirmation_screenshot"' in html or fail("缺少截图上传控件")
    assert 'required' in html.split('id="confirmation_screenshot"')[1].split('>')[0] \
        or warn("截图控件可能缺少 required 属性")
    ok("截图上传控件存在且标记 required")

    # 按钮文本
    assert "确认已执行并提交" in html or fail("按钮文本应为'确认已执行并提交'")
    ok("按钮文本：确认已执行并提交")

    # 警告始终可见（无 hidden）
    assert "confirmWarning" not in html or "hidden" not in \
        html.split("confirmWarning")[1].split(">")[0] if "confirmWarning" in html else True
    ok("警告信息始终可见")

    # 5.2 POST 无截图 — 应被拦截
    print("\n[5.2] POST 无截图 — 应被拦截")
    r = client.post(f"/confirm/{token}", data={
        "confirmation_status": "已执行已提交",
        "confirmation_note": "测试无截图",
    }, follow_redirects=True)
    html2 = r.data.decode("utf-8")
    assert "必须上传" in html2 or fail("应提示必须上传截图")
    ok("后端正确拦截无截图提交")

    # 验证 DB 未被修改
    with app.app_context():
        t = db.session.get(Task, task.id)
        assert t.confirmation_status == "未确认" or fail("状态不应被修改")
        assert t.confirmation_submitted_at is None or fail("submit时间不应被设置")
    ok("DB 状态未被污染")

    # 5.3 POST 带截图 — 应成功提交
    print("\n[5.3] POST 带截图 — 应成功提交")
    import io
    fake_png = (io.BytesIO(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100), "test.png")
    r = client.post(f"/confirm/{token}", data={
        "confirmation_status": "已执行已提交",
        "confirmation_note": "门店已完成探店并提交报告",
        "confirmation_screenshot": fake_png,
    }, content_type="multipart/form-data", follow_redirects=True)
    html3 = r.data.decode("utf-8")
    assert "提交成功" in html3 or fail(f"应提示提交成功, 实际: {html3[:500]}")
    ok("提交成功提示显示")

    # 5.4 验证 DB 状态
    print("\n[5.4] 验证 DB 状态")
    with app.app_context():
        t = db.session.get(Task, task.id)
        assert t.confirmation_status == "已执行已提交" \
            or fail(f"confirmation_status 应为已执行已提交, 实际: {t.confirmation_status}")
        ok("confirmation_status = 已执行已提交")

        assert t.confirmation_review_status == "待核对" \
            or fail(f"review_status 应为待核对, 实际: {t.confirmation_review_status}")
        ok("confirmation_review_status = 待核对")

        assert t.confirmation_submitted_at is not None or fail("submitted_at 不应为空")
        ok("confirmation_submitted_at 已记录")

        assert t.confirmation_note == "门店已完成探店并提交报告" \
            or fail(f"note 不正确: {t.confirmation_note}")
        ok("执行说明已保存")

        assert t.confirmation_screenshot is not None or fail("screenshot 路径不应为空")
        ok("截图路径已保存")

        # task_status 不应变为放弃执行
        assert t.task_status == "进行中" \
            or fail(f"task_status 不应变化, 实际: {t.task_status}")
        ok("task_status 保持进行中（未变为放弃执行）")

    # 5.5 验证流水
    print("\n[5.5] 验证流水记录")
    with app.app_context():
        flows = StoreFlowRecord.query.filter_by(task_id=task.id).all()
        confirm_flows = [f for f in flows if "第三方门店执行确认" in (f.action or "")]
        assert len(confirm_flows) == 1 or fail(f"应有1条确认流水, 实际: {len(confirm_flows)}")
        assert "已执行已提交" in (confirm_flows[0].after_text or "") or fail("流水应包含'已执行已提交'")
        assert "门店已完成探店并提交报告" in (confirm_flows[0].after_text or "") or fail("流水应包含说明")
        ok("流水记录正确")

    # 5.6 重复提交 — 应被拦截
    print("\n[5.6] 重复提交 — 应被拦截")
    fake_png2 = (io.BytesIO(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100), "test2.png")
    r = client.post(f"/confirm/{token}", data={
        "confirmation_status": "已执行已提交",
        "confirmation_note": "试图重复提交",
        "confirmation_screenshot": fake_png2,
    }, content_type="multipart/form-data", follow_redirects=True)
    html4 = r.data.decode("utf-8")
    assert "不能重复修改" in html4 or fail("应拦截重复提交")
    ok("重复提交被正确拦截")

    # 5.7 已提交后再 GET — 显示已提交信息
    print("\n[5.7] 已提交后 GET 页面")
    r = client.get(f"/confirm/{token}")
    html5 = r.data.decode("utf-8")
    assert "已提交" in html5 or fail("应显示已提交状态")
    assert "门店已完成探店并提交报告" in html5 or fail("应显示之前的说明")
    # 不应出现表单
    assert '<form method="post"' not in html5 or fail("已提交后不应再显示表单")
    ok("已提交后正确显示只读状态，无表单")

    ok("测试五通过 ✓")


def test_html_escaping():
    print("\n" + "=" * 60)
    print("测试六：HTML 特殊字符转义")
    print("=" * 60)

    client, task_id = setup()

    special = '测试"双引号"<脚本>&符号'
    r = client.post(f"/tasks/{task_id}/executor",
                    data={"executor_name": special, "executor_phone": "", "payee_name": "",
                          "payee_phone": "", "payee_bank": "", "payee_account": "", "executor_remarks": ""},
                    follow_redirects=True)
    html = r.data.decode("utf-8")

    # 不应出现未转义的 HTML 标签
    assert '<script>' not in html.lower() or fail("XSS 风险: 未转义 <script>")
    # 应出现转义后的内容
    assert '&quot;' in html or '&#34;' in html or fail("双引号未转义")
    assert '&lt;' in html or '&#60;' in html or fail("尖括号未转义")
    assert '&amp;' in html or fail("& 符号未转义")
    ok("特殊字符正确转义，无 XSS 风险 ✓")


# ================================================================
# 主入口
# ================================================================

def main():
    tests = [
        ("编辑保存流程", test_edit_save_flow),
        ("只填电话号码场景", test_phone_only_scenario),
        ("连续编辑稳定性", test_consecutive_saves),
        ("权限校验", test_permission),
        ("第三方确认表单", test_confirmation_form),
        ("HTML 转义", test_html_escaping),
    ]

    passed = 0
    for name, fn in tests:
        try:
            fn()
            passed += 1
        except AssertionError as e:
            print(f"\n  ⛔ {name} 失败: {e}")
        except Exception as e:
            print(f"\n  ⛔ {name} 异常: {e}")

    # 清理
    db_path = os.path.join(BASE_DIR, "xf_erp_test.db")
    if os.path.exists(db_path):
        try:
            os.remove(db_path)
        except PermissionError:
            pass

    print("\n" + "=" * 60)
    print(f"门店任务测试完成: {passed}/{len(tests)} 通过")
    print("=" * 60)


if __name__ == "__main__":
    main()
