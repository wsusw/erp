"""
先锋探店 ERP 自检脚本
运行方式：python test_system.py
说明：脚本使用独立的 xf_erp_test.db，不会清空正式 xf_erp.db。
"""
import os
from io import BytesIO

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(BASE_DIR, "xf_erp_test.db")
os.environ["ERP_SECRET_KEY"] = "test-secret"

from app import app, db, seed_data, Employee, Task, User, RejectReason  # noqa: E402


def assert_ok(condition, message):
    if not condition:
        raise AssertionError(message)
    print("✅", message)


def main():
    with app.app_context():
        db.drop_all()
        db.create_all()
        seed_data()
        assert_ok(User.query.count() >= 3, "默认账号已生成")
        task = Task.query.first()
        assert_ok(task is not None, "演示任务已生成")
        task_id = task.id
        token = task.confirmation_token

    client = app.test_client()
    r = client.post("/login", data={"username": "admin", "password": "admin123"}, follow_redirects=True)
    assert_ok(r.status_code == 200 and "工作台".encode("utf-8") in r.data, "超管登录成功")

    for path in ["/", "/tasks", "/reports", "/projects", "/employees", "/users", "/settings", f"/tasks/{task_id}"]:
        r = client.get(path)
        assert_ok(r.status_code == 200, f"页面可访问：{path}")

    r = client.post(f"/tasks/{task_id}/confirmation/start", follow_redirects=True)
    assert_ok(r.status_code == 200, "可发起门店执行确认")

    r = client.post(
        f"/tasks/{task_id}/confirmation/sent",
        data={"sent_to": "测试神秘顾客 13800000000", "sent_note": "测试发送"},
        follow_redirects=True,
    )
    assert_ok(r.status_code == 200, "可标记确认链接已发送")

    r = client.post(
        f"/confirm/{token}",
        data={"confirmation_status": "已执行已提交", "confirmation_note": "未传截图"},
        follow_redirects=True,
    )
    assert_ok("必须上传".encode("utf-8") in r.data, "后端强制校验已执行已提交截图")

    r = client.post(
        f"/confirm/{token}",
        data={
            "confirmation_status": "已执行已提交",
            "confirmation_note": "已提交截图",
            "confirmation_screenshot": (BytesIO(b"fake-image"), "shot.jpg"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert_ok(r.status_code == 200 and "提交成功".encode("utf-8") in r.data, "第三方确认可提交截图")

    with app.app_context():
        task = db.session.get(Task, task_id)
        assert_ok(task.confirmation_review_status == "待核对", "截图提交后进入待核对")
        assert_ok(bool(task.confirmation_screenshot), "确认截图已保存")

    r = client.post(
        f"/tasks/{task_id}/confirmation/review",
        data={"decision": "pass", "review_note": "截图清晰，通过"},
        follow_redirects=True,
    )
    assert_ok(r.status_code == 200, "后台可核对确认截图")

    r = client.post(f"/tasks/{task_id}/confirmation/regenerate", follow_redirects=True)
    assert_ok(r.status_code == 200, "可重新生成确认链接")
    r = client.get(f"/confirm/{token}")
    assert_ok(r.status_code == 404, "旧确认链接已作废")

    r = client.post(
        "/tasks/create",
        data={"store_name": "日期错误门店", "start_time": "2026-02-10", "end_time": "2026-02-01"},
        follow_redirects=True,
    )
    assert_ok("截止时间不能早于开始时间".encode("utf-8") in r.data, "任务创建日期校验生效")

    with app.app_context():
        task = db.session.get(Task, task_id)
        task.task_status = "已完成"
        db.session.commit()
    r = client.post(
        f"/tasks/{task_id}/result",
        data={"problem_list": "重复提交测试", "result_description": "不应允许"},
        follow_redirects=True,
    )
    assert_ok("任务已完结，无法再次提交结果".encode("utf-8") in r.data, "已完成任务禁止再次提交结果，避免状态倒退")
    with app.app_context():
        task = db.session.get(Task, task_id)
        assert_ok(task.task_status == "已完成", "重复提交不会把已完成任务回退为待主管审核")
        task.task_status = "放弃执行"
        db.session.commit()
    r = client.post(f"/tasks/{task_id}/accept", follow_redirects=True)
    assert_ok("任务已完结，无法再次承接".encode("utf-8") in r.data, "已完结任务禁止再次承接")

    client.get("/logout", follow_redirects=True)
    r = client.post("/login", data={"username": "operator", "password": "operator123"}, follow_redirects=True)
    assert_ok(r.status_code == 200, "运营账号登录成功")
    r = client.post(f"/tasks/{task_id}/confirmation/start", follow_redirects=True)
    assert_ok(r.status_code == 200 and "已发起门店执行确认".encode("utf-8") in r.data, "运营可发起本人任务的门店执行确认")
    r = client.post(
        f"/tasks/{task_id}/confirmation/sent",
        data={"sent_to": "运营发送对象", "sent_note": "运营标记发送"},
        follow_redirects=True,
    )
    assert_ok(r.status_code == 200 and "已记录确认链接发送信息".encode("utf-8") in r.data, "运营可标记确认链接已发送")
    r = client.post(
        "/tasks/create",
        data={"store_name": "运营越权建单", "start_time": "2026-01-01", "end_time": "2026-01-02"},
        follow_redirects=True,
    )
    assert_ok("当前账号没有权限执行该操作".encode("utf-8") in r.data, "运营账号不能主动新建任务")

    client.get("/logout", follow_redirects=True)
    r = client.post("/login", data={"username": "supervisor", "password": "supervisor123"}, follow_redirects=True)
    assert_ok(r.status_code == 200, "主管账号登录成功")
    r = client.post(
        "/employees",
        data={"name": "主管新增运营", "phone": "13900000001", "monthly_target": "25", "position": "主管不应自定义"},
        follow_redirects=True,
    )
    assert_ok(r.status_code == 200 and "人员档案已创建".encode("utf-8") in r.data, "主管可新增自己的分管运营人员档案")
    with app.app_context():
        emp = Task.query.first().operator.supervisor.operators[-1]
        assert_ok(emp.name == "主管新增运营" and emp.position == "运营", "主管新增人员自动归属当前主管并固定为运营档案")

    client.get("/logout", follow_redirects=True)
    client.post("/login", data={"username": "admin", "password": "admin123"}, follow_redirects=True)

    r = client.get("/tasks/template")
    assert_ok(b"agency_price" in r.data, "批量导入模板包含代理价格 agency_price 字段")
    assert_ok(b"supervisor_name" in r.data and b"operator_name" in r.data, "批量导入模板使用姓名字段")
    r = client.post(
        "/tasks/import",
        data={
            "csv_file": (
                BytesIO(
                    "store_name,region,address,urgency,start_time,end_time,payment_base_price,agency_price,supervisor_name,operator_name,store_remarks,task_sop_html\n"
                    "姓名导入门店,华东一区,上海市测试路1号,一般,2026-07-18,2026-07-25,35,80,李主管,王运营,备注,<p>SOP</p>\n".encode("utf-8")
                ),
                "tasks-by-name.csv",
            )
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert_ok(r.status_code == 200 and "成功 1 条".encode("utf-8") in r.data, "批量导入可按员工姓名识别主管和运营")
    with app.app_context():
        imported = Task.query.filter_by(store_name="姓名导入门店").first()
        supervisor_emp = Employee.query.filter_by(name="李主管").first()
        operator_emp = Employee.query.filter_by(name="王运营").first()
        assert_ok(imported and imported.supervisor_id == supervisor_emp.id and imported.operator_id == operator_emp.id, "姓名导入写入正确员工ID")
    r = client.post(
        "/tasks/import",
        data={
            "csv_file": (
                BytesIO(
                    "store_name,region,address,urgency,start_time,end_time,payment_base_price,agency_price,supervisor_id,operator_id,store_remarks,task_sop_html\n"
                    "账号兼容导入门店,华东一区,上海市测试路2号,一般,2026-07-18,2026-07-25,35,80,supervisor,operator,备注,<p>SOP</p>\n".encode("utf-8")
                ),
                "tasks-by-legacy-account.csv",
            )
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert_ok(r.status_code == 200 and "成功 1 条".encode("utf-8") in r.data, "旧 supervisor_id/operator_id 列可兼容登录账号")
    r = client.get("/tasks")
    assert_ok(b"min_agency" not in r.data and b"max_agency" not in r.data, "任务筛选区不显示代理价格筛选字段")
    assert_ok("待确认".encode("utf-8") in r.data, "任务筛选区包含待确认状态")

    r = client.post(
        "/tasks/batch",
        data={"task_ids": [str(task_id)], "batch_task_status": "待确认"},
        follow_redirects=True,
    )
    assert_ok(r.status_code == 200 and "已批量更新".encode("utf-8") in r.data, "批量设置可更新任务状态")
    with app.app_context():
        task = db.session.get(Task, task_id)
        assert_ok(task.task_status == "待确认", "批量设置已写入待确认状态")
    r = client.get("/tasks?status=待确认")
    assert_ok(r.status_code == 200 and "待确认".encode("utf-8") in r.data, "精准筛选可按待确认状态查询")

    # 确认流程自动联动 task_status → 待确认
    r = client.post(
        "/tasks/create",
        data={"store_name": "待确认联动测试", "start_time": "2026-06-01", "end_time": "2026-06-15", "payment_base_price": "100"},
        follow_redirects=True,
    )
    assert_ok(r.status_code == 200, "超管可创建确认联动测试任务")
    with app.app_context():
        ct = Task.query.filter_by(store_name="待确认联动测试").first()
        assert_ok(ct is not None, "确认联动测试任务已入库")
        ct_id = ct.id
        ct_token = ct.confirmation_token or "will-be-set"
        # 确保任务处于可确认状态
        ct.task_status = "进行中"
        db.session.commit()
    # 发起确认 → 应自动设为 待确认
    r = client.post(f"/tasks/{ct_id}/confirmation/start", follow_redirects=True)
    with app.app_context():
        ct = db.session.get(Task, ct_id)
        assert_ok(ct.task_status == "待确认", "发起门店确认后任务状态自动变为待确认")
    # 第三方提交确认
    with app.app_context():
        ct = db.session.get(Task, ct_id)
        ct_token = ct.confirmation_token
    r = client.post(
        f"/confirm/{ct_token}",
        data={"confirmation_status": "已执行已提交", "confirmation_note": "联动测试",
              "confirmation_screenshot": (BytesIO(b"fake"), "shot.jpg")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert_ok(r.status_code == 200 and "提交成功".encode("utf-8") in r.data, "第三方可提交确认联动测试")
    # 审核通过 → 应从待确认变为已完成
    r = client.post(
        f"/tasks/{ct_id}/confirmation/review",
        data={"decision": "pass", "review_note": "联动测试通过"},
        follow_redirects=True,
    )
    with app.app_context():
        ct = db.session.get(Task, ct_id)
        assert_ok(ct.task_status == "已完成", "确认审核通过后任务状态从待确认自动变为已完成")
        assert_ok(ct.confirmation_review_status == "截图核对通过", "确认审核状态为截图核对通过")

    # 第二组：审核驳回 → 待确认 → 已退回
    r = client.post(
        "/tasks/create",
        data={"store_name": "待确认驳回测试", "start_time": "2026-07-01", "end_time": "2026-07-15", "payment_base_price": "200"},
        follow_redirects=True,
    )
    with app.app_context():
        ct2 = Task.query.filter_by(store_name="待确认驳回测试").first()
        ct2.task_status = "进行中"
        db.session.commit()
        ct2_id = ct2.id
    r = client.post(f"/tasks/{ct2_id}/confirmation/start", follow_redirects=True)
    with app.app_context():
        ct2 = db.session.get(Task, ct2_id)
        assert_ok(ct2.task_status == "待确认", "发起确认后第二个测试任务也自动变为待确认")
        ct2_token = ct2.confirmation_token
    r = client.post(
        f"/confirm/{ct2_token}",
        data={"confirmation_status": "已执行已提交", "confirmation_note": "驳回测试",
              "confirmation_screenshot": (BytesIO(b"fake2"), "shot2.jpg")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    r = client.post(
        f"/tasks/{ct2_id}/confirmation/review",
        data={"decision": "reject", "review_note": "截图模糊，驳回"},
        follow_redirects=True,
    )
    with app.app_context():
        ct2 = db.session.get(Task, ct2_id)
        assert_ok(ct2.task_status == "已退回", "确认审核驳回后任务状态从待确认自动变为已退回")
        assert_ok(ct2.confirmation_review_status == "截图核对驳回", "确认审核状态为截图核对驳回")

    r = client.post(
        "/tasks/create",
        data={"store_name": "待作废门店", "start_time": "2026-01-01", "end_time": "2026-01-02", "payment_base_price": "50"},
        follow_redirects=True,
    )
    assert_ok(r.status_code == 200, "超管可创建待作废测试任务")
    with app.app_context():
        void_task_obj = Task.query.filter_by(store_name="待作废门店").first()
        assert_ok(void_task_obj is not None, "待作废任务已入库")
        void_task_id = void_task_obj.id
    r = client.post(f"/tasks/{void_task_id}/void", data={"void_reason": "自检作废"}, follow_redirects=True)
    assert_ok(r.status_code == 200 and "门店任务已作废".encode("utf-8") in r.data, "超管可作废任务而非物理删除")
    with app.app_context():
        void_task_obj = db.session.get(Task, void_task_id)
        assert_ok(void_task_obj is not None and void_task_obj.is_voided and void_task_obj.task_status == "已作废", "作废任务数据库保留并标记作废")
    r = client.get("/tasks")
    assert_ok("待作废门店".encode("utf-8") not in r.data, "作废任务不再显示于任务池")

    csv_with_agency = "store_name,region,address,urgency,start_time,end_time,payment_base_price,agency_price,supervisor_id,operator_id,store_remarks,task_sop_html\n代理价导入门店,华东,地址,一般,2026-01-01,2026-01-02,50,88.88,,,,<p>SOP</p>\n"
    r = client.post(
        "/tasks/import",
        data={"csv_file": (BytesIO(csv_with_agency.encode("utf-8-sig")), "agency_tasks.csv")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert_ok(r.status_code == 200 and "成功 1 条".encode("utf-8") in r.data, "超管批量导入可解析代理价格字段")
    with app.app_context():
        t = Task.query.filter_by(store_name="代理价导入门店").first()
        assert_ok(t is not None and abs((t.agency_price or 0) - 88.88) < 0.01, "代理价格已随批量导入写入任务")

    csv_text = "store_name,region,address,urgency,start_time,end_time,payment_base_price,supervisor_id,operator_id,store_remarks,task_sop_html\n导入门店A,华东,地址,一般,2026-01-01,2026-01-02,50,,,,<p>SOP</p>\n导入门店B,华东,地址,一般,2026-02-10,2026-02-01,50,,,,<p>SOP</p>\n"
    r = client.post(
        "/tasks/import",
        data={"csv_file": (BytesIO(csv_text.encode("utf-8-sig")), "tasks.csv")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert_ok(r.status_code == 200 and "失败".encode("utf-8") in r.data, "批量导入成功/失败反馈生效")

    csv_bad_price = "store_name,region,address,urgency,start_time,end_time,payment_base_price,supervisor_id,operator_id,store_remarks,task_sop_html\n零元脏数据门店,华东,地址,一般,2026-01-01,2026-01-02,abc,,,,<p>SOP</p>\n"
    r = client.post(
        "/tasks/import",
        data={"csv_file": (BytesIO(csv_bad_price.encode("utf-8-sig")), "bad_price.csv")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert_ok("打款基准价格式错误".encode("utf-8") in r.data, "批量导入会拦截非数字金额，避免静默变成 0 元")

    r = client.post("/settings", data={"category": "其他", "content": "测试驳回理由"}, follow_redirects=True)
    assert_ok(r.status_code == 200, "可添加常用驳回理由")
    with app.app_context():
        reason = RejectReason.query.filter_by(content="测试驳回理由").first()
        assert_ok(reason is not None, "理由已入库")
        reason_id = reason.id
    r = client.post(f"/settings/reasons/{reason_id}/delete", follow_redirects=True)
    assert_ok(r.status_code == 200, "可删除常用驳回理由")

    for path in ["/export/tasks", "/export/confirmations", "/export/flows", "/export/monthly"]:
        r = client.get(path)
        assert_ok(r.status_code == 200 and "text/csv" in r.headers.get("Content-Type", ""), f"CSV 导出正常：{path}")

    print("\n全部自检通过。")


if __name__ == "__main__":
    main()
