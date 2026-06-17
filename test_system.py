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

from app import app, db, seed_data, Task, User, RejectReason  # noqa: E402


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
    r = client.get("/tasks")
    assert_ok(b"min_agency" not in r.data and b"max_agency" not in r.data, "任务筛选区不显示代理价格筛选字段")

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
