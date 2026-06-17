import csv
import io
import os
import secrets
import re
from datetime import date, datetime, timezone
from functools import wraps
from typing import Optional

from flask import (
    Flask, Response, abort, flash, redirect, render_template, request,
    send_from_directory, url_for
)
from flask_login import (
    LoginManager, UserMixin, current_user, login_required, login_user, logout_user
)
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import and_, or_
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

# ============================================================
# 先锋探店 ERP 完整可执行版本
# ------------------------------------------------------------
# 设计目标：
# 1) 严格落地老板需求中的三级账号、任务指派、审批、门店确认、SOP、流转记录、报表导出。
# 2) 保持单文件 Flask 结构，降低部署和二次修改难度。
# 3) 前端采用 AppleMusicBeta 风格：白底、高留白、品牌蓝、轻量表格和卡片。
# 4) 默认 SQLite 数据库，启动即自动建表和种子账号，方便老板直接验收。
# ============================================================

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("ERP_SECRET_KEY", "xf-erp-dev-secret-change-in-production")
app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get(
    "DATABASE_URL", "sqlite:///" + os.path.join(BASE_DIR, "xf_erp.db")
)
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["UPLOAD_FOLDER"] = UPLOAD_DIR
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024

# 默认不信任 X-Forwarded-For，避免公开确认页限流被伪造请求头绕过。
# 如果生产环境部署在 Nginx / 网关等可信反向代理后，请设置 TRUST_PROXY=1，
# 由 Werkzeug ProxyFix 在框架层修正 request.remote_addr，业务代码仍统一使用 remote_addr。
if os.environ.get("TRUST_PROXY", "0") == "1":
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

ALLOWED_UPLOAD_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp", "pdf", "mp4", "mov", "xlsx", "xls", "csv", "doc", "docx"}

ROLE_LABELS = {
    "super_admin": "超级管理员",
    "supervisor": "主管",
    "operator": "业务运营",
}

TASK_STATUSES = [
    "待主管承接", "待主管分配", "待运营承接", "进行中", "待主管审核", "已完成", "已退回", "异常上报", "放弃执行"
]

CONFIRMATION_STATUSES = ["未确认", "已执行待提交", "待执行", "放弃执行", "已执行已提交"]
CONFIRMATION_REVIEW_STATUSES = ["未发起", "待第三方提交", "待核对", "截图核对通过", "截图核对驳回", "无需核对", "链接已作废"]

PRICE_STATUS = ["已自动通过", "待主管审批", "主管已通过待超管审批", "已通过", "已驳回"]

TRAVEL_STATUS = ["待主管审批", "主管已通过待超管审批", "已通过", "已驳回"]

REJECT_CATEGORIES = ["任务审核", "价格调整", "路费补贴", "门店确认", "其他"]


db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message = "请先登录系统"

# 公开确认页的简单内存级提交限流：同一 IP 在 10 分钟内最多提交 12 次。
# 生产环境建议替换为 Flask-Limiter + Redis。
CONFIRM_RATE_LIMIT = {}
CONFIRM_RATE_WINDOW_SECONDS = 10 * 60
CONFIRM_RATE_MAX_POSTS = 12


def utc_now():
    """统一使用带 UTC 时区的当前时间，避免 datetime.utcnow() 的弃用问题。"""
    return datetime.now(timezone.utc)


# ============================================================
# 数据模型
# ============================================================
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(32), default="operator", nullable=False, index=True)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    employee_id = db.Column(db.Integer, db.ForeignKey("employee.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now)

    employee = db.relationship("Employee", back_populates="user", uselist=False, foreign_keys=[employee_id])

    def set_password(self, raw_password: str) -> None:
        self.password_hash = generate_password_hash(raw_password)

    def check_password(self, raw_password: str) -> bool:
        return check_password_hash(self.password_hash, raw_password)

    @property
    def display_name(self) -> str:
        return self.employee.name if self.employee else self.username

    @property
    def role_label(self) -> str:
        return ROLE_LABELS.get(self.role, self.role)


class Employee(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(64), nullable=False)
    phone = db.Column(db.String(32), default="")
    position = db.Column(db.String(64), default="")
    supervisor_id = db.Column(db.Integer, db.ForeignKey("employee.id"), nullable=True, index=True)
    monthly_target = db.Column(db.Integer, default=30, nullable=False)
    created_at = db.Column(db.DateTime, default=utc_now)

    user = db.relationship("User", back_populates="employee", uselist=False, foreign_keys="User.employee_id")
    supervisor = db.relationship("Employee", remote_side=[id], backref="operators")

    @property
    def role_label(self) -> str:
        return self.user.role_label if self.user else "未绑定账号"

    @property
    def account_status(self) -> str:
        if not self.user:
            return "未绑定"
        return "启用" if self.user.is_active else "禁用"


class Project(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    project_name = db.Column(db.String(128), nullable=False)
    description = db.Column(db.Text, default="")
    payment_status = db.Column(db.String(32), default="待打款", nullable=False)
    created_at = db.Column(db.DateTime, default=utc_now)


class Task(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(64), unique=True, nullable=False, index=True)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id"), nullable=False, index=True)
    creator_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    supervisor_id = db.Column(db.Integer, db.ForeignKey("employee.id"), nullable=True, index=True)
    operator_id = db.Column(db.Integer, db.ForeignKey("employee.id"), nullable=True, index=True)

    store_name = db.Column(db.String(128), nullable=False, index=True)
    region = db.Column(db.String(64), default="未分区")
    address = db.Column(db.String(255), default="")
    urgency = db.Column(db.String(32), default="一般")
    start_time = db.Column(db.Date, nullable=False)
    end_time = db.Column(db.Date, nullable=False)

    # 打款价格由系统计算：基准价 + 已通过加价。代理价仅超管可见和编辑。
    payment_base_price = db.Column(db.Float, default=0.0, nullable=False)
    approved_extra_price = db.Column(db.Float, default=0.0, nullable=False)
    agency_price = db.Column(db.Float, nullable=True)

    task_sop_html = db.Column(db.Text, default="")
    store_remarks = db.Column(db.Text, default="")
    task_status = db.Column(db.String(32), default="待主管承接", nullable=False, index=True)
    audit_status = db.Column(db.String(32), default="待审核", nullable=False)
    payment_status = db.Column(db.String(32), default="待打款", nullable=False)

    executor_name = db.Column(db.String(64), default="")
    executor_phone = db.Column(db.String(32), default="")
    payee_name = db.Column(db.String(64), default="")
    payee_phone = db.Column(db.String(32), default="")
    payee_account = db.Column(db.String(128), default="")
    payee_bank = db.Column(db.String(128), default="")
    executor_remarks = db.Column(db.Text, default="")

    confirmation_token = db.Column(db.String(64), unique=True, index=True)
    confirmation_status = db.Column(db.String(32), default="未确认")
    confirmation_note = db.Column(db.Text, default="")
    confirmation_screenshot = db.Column(db.String(255), default="")
    confirmation_submitted_at = db.Column(db.DateTime, nullable=True)
    confirmation_started_at = db.Column(db.DateTime, nullable=True)
    confirmation_sent_at = db.Column(db.DateTime, nullable=True)
    confirmation_sent_to = db.Column(db.String(128), default="")
    confirmation_sent_note = db.Column(db.Text, default="")
    confirmation_review_status = db.Column(db.String(32), default="未发起", index=True)
    confirmation_review_note = db.Column(db.Text, default="")
    confirmation_reviewed_by = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    confirmation_reviewed_at = db.Column(db.DateTime, nullable=True)

    confirmation_reviewer = db.relationship("User", foreign_keys=[confirmation_reviewed_by])

    exception_summary = db.Column(db.Text, default="")
    created_at = db.Column(db.DateTime, default=utc_now, index=True)
    updated_at = db.Column(db.DateTime, default=utc_now, onupdate=utc_now)

    project = db.relationship("Project", backref="tasks")
    creator = db.relationship("User", foreign_keys=[creator_id])
    supervisor = db.relationship("Employee", foreign_keys=[supervisor_id])
    operator = db.relationship("Employee", foreign_keys=[operator_id])

    @property
    def final_payment_price(self) -> float:
        return round((self.payment_base_price or 0) + (self.approved_extra_price or 0), 2)

    @property
    def countdown(self) -> int:
        # 截止当天显示“剩余 1 天”，避免用户误以为当天已经过期。
        if self.end_time < date.today():
            return 0
        return (self.end_time - date.today()).days + 1

    @property
    def is_overdue(self) -> bool:
        return self.end_time < date.today() and self.task_status != "已完成"


class MysteryShopper(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    task_id = db.Column(db.Integer, db.ForeignKey("task.id"), nullable=False, index=True)
    name = db.Column(db.String(64), nullable=False)
    phone = db.Column(db.String(32), default="")
    identity_note = db.Column(db.String(255), default="")
    status = db.Column(db.String(32), default="待执行")
    created_by = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    created_at = db.Column(db.DateTime, default=utc_now)

    task = db.relationship("Task", backref="mystery_shoppers")
    creator = db.relationship("User", foreign_keys=[created_by])


class TaskResult(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    task_id = db.Column(db.Integer, db.ForeignKey("task.id"), nullable=False, index=True)
    submitted_by = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    problem_list = db.Column(db.Text, default="")
    result_description = db.Column(db.Text, default="")
    screenshot_path = db.Column(db.String(255), default="")
    status = db.Column(db.String(32), default="待审核")
    review_comment = db.Column(db.Text, default="")
    reviewed_by = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now)
    reviewed_at = db.Column(db.DateTime, nullable=True)

    task = db.relationship("Task", backref=db.backref("results", order_by="TaskResult.created_at.desc()"))
    submitter = db.relationship("User", foreign_keys=[submitted_by])
    reviewer = db.relationship("User", foreign_keys=[reviewed_by])


class PriceAdjustment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    task_id = db.Column(db.Integer, db.ForeignKey("task.id"), nullable=False, index=True)
    applicant_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    amount_change = db.Column(db.Float, nullable=False)
    reason = db.Column(db.Text, default="")
    status = db.Column(db.String(64), default="待主管审批", index=True)
    supervisor_comment = db.Column(db.Text, default="")
    admin_comment = db.Column(db.Text, default="")
    created_at = db.Column(db.DateTime, default=utc_now)
    supervisor_reviewed_at = db.Column(db.DateTime, nullable=True)
    admin_reviewed_at = db.Column(db.DateTime, nullable=True)

    task = db.relationship("Task", backref=db.backref("price_adjustments", order_by="PriceAdjustment.created_at.desc()"))
    applicant = db.relationship("User", foreign_keys=[applicant_id])


class TravelSubsidy(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    task_id = db.Column(db.Integer, db.ForeignKey("task.id"), nullable=False, index=True)
    applicant_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    reason = db.Column(db.Text, default="")
    voucher_path = db.Column(db.String(255), default="")
    status = db.Column(db.String(64), default="待主管审批", index=True)
    supervisor_comment = db.Column(db.Text, default="")
    admin_comment = db.Column(db.Text, default="")
    created_at = db.Column(db.DateTime, default=utc_now)
    supervisor_reviewed_at = db.Column(db.DateTime, nullable=True)
    admin_reviewed_at = db.Column(db.DateTime, nullable=True)

    task = db.relationship("Task", backref=db.backref("travel_subsidies", order_by="TravelSubsidy.created_at.desc()"))
    applicant = db.relationship("User", foreign_keys=[applicant_id])


class RejectReason(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    category = db.Column(db.String(32), default="其他")
    content = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=utc_now)

    user = db.relationship("User", backref="reject_reasons")


class StoreFlowRecord(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    task_id = db.Column(db.Integer, db.ForeignKey("task.id"), nullable=False, index=True)
    operator_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    operator_name = db.Column(db.String(64), default="系统")
    action = db.Column(db.String(64), nullable=False)
    before_text = db.Column(db.Text, default="")
    after_text = db.Column(db.Text, default="")
    created_at = db.Column(db.DateTime, default=utc_now, index=True)

    task = db.relationship("Task", backref=db.backref("flow_records", order_by="StoreFlowRecord.created_at.desc()"))
    operator = db.relationship("User", foreign_keys=[operator_id])


class OperationLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    operator_name = db.Column(db.String(64), default="系统")
    module = db.Column(db.String(64), default="")
    action = db.Column(db.String(64), default="")
    detail = db.Column(db.Text, default="")
    created_at = db.Column(db.DateTime, default=utc_now, index=True)


# ============================================================
# 登录与通用工具
# ============================================================
@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


def parse_date(value: str, default: Optional[date] = None) -> Optional[date]:
    if not value:
        return default
    normalized = value.strip().replace("/", "-")
    return datetime.strptime(normalized, "%Y-%m-%d").date()


def dates_are_valid(start_value: str, end_value: str):
    """解析并校验任务起止日期，返回 (start, end, error_message)。"""
    try:
        start = parse_date(start_value, date.today())
        end = parse_date(end_value, date.today())
    except (TypeError, ValueError):
        return None, None, "日期格式不正确，请使用系统日期选择器重新选择。"
    if end < start:
        return None, None, "截止时间不能早于开始时间。"
    return start, end, ""


def parse_float(value: str, default: float = 0.0) -> float:
    try:
        return float(value) if value not in (None, "") else default
    except ValueError:
        return default


INVALID_TEXT_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def has_invalid_text(value: str) -> bool:
    """检测 CSV 导入中的控制字符，避免异常字符污染门店名称、地址等核心字段。"""
    return bool(value and INVALID_TEXT_RE.search(value))


def parse_required_positive_float(value: str, field_label: str):
    """导入场景专用：金额字段必须存在、必须是数字、必须大于 0。"""
    raw = (value or "").strip()
    if not raw:
        return None, f"{field_label}不能为空"
    try:
        amount = float(raw)
    except ValueError:
        return None, f"{field_label}格式错误，必须是数字"
    if amount <= 0:
        return None, f"{field_label}必须大于 0"
    return amount, ""


def role_required(*roles):
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if not current_user.is_authenticated or current_user.role not in roles:
                flash("❌ 当前账号没有权限执行该操作", "danger")
                return redirect(url_for("dashboard"))
            return fn(*args, **kwargs)
        return wrapper
    return decorator


def current_employee_id() -> Optional[int]:
    return current_user.employee_id if current_user.is_authenticated else None


def visible_tasks_query():
    """按角色返回可见任务。越权问题优先在后端解决，前端只做辅助隐藏。"""
    q = Task.query
    if current_user.role == "super_admin":
        return q
    if current_user.role == "supervisor":
        emp_id = current_employee_id()
        if not emp_id:
            return q.filter(False)
        operator_ids = [e.id for e in Employee.query.filter_by(supervisor_id=emp_id).all()]
        return q.filter(or_(Task.supervisor_id == emp_id, Task.operator_id.in_(operator_ids)))
    emp_id = current_employee_id()
    if not emp_id:
        return q.filter(False)
    return q.filter(or_(Task.operator_id == emp_id, Task.creator_id == current_user.id))


def can_access_task(task: Task) -> bool:
    return visible_tasks_query().filter(Task.id == task.id).first() is not None


def can_manage_task(task: Task) -> bool:
    if current_user.role == "super_admin":
        return True
    if current_user.role == "supervisor":
        return task.supervisor_id == current_employee_id()
    if current_user.role == "operator":
        return task.operator_id == current_employee_id() or task.creator_id == current_user.id
    return False


def allowed_operators_for_current_user():
    if current_user.role == "super_admin":
        return Employee.query.join(User, User.employee_id == Employee.id).filter(User.role == "operator").order_by(Employee.name.asc()).all()
    if current_user.role == "supervisor":
        return Employee.query.join(User, User.employee_id == Employee.id).filter(
            User.role == "operator", Employee.supervisor_id == current_employee_id()
        ).order_by(Employee.name.asc()).all()
    return Employee.query.filter(Employee.id == current_employee_id()).all()


def allowed_supervisors_for_current_user():
    if current_user.role == "super_admin":
        return Employee.query.join(User, User.employee_id == Employee.id).filter(User.role == "supervisor").order_by(Employee.name.asc()).all()
    if current_user.role == "supervisor" and current_user.employee:
        return [current_user.employee]
    if current_user.role == "operator" and current_user.employee and current_user.employee.supervisor:
        return [current_user.employee.supervisor]
    return []


def log_operation(module: str, action: str, detail: str) -> None:
    operator_name = current_user.display_name if current_user.is_authenticated else "系统"
    db.session.add(OperationLog(operator_name=operator_name, module=module, action=action, detail=detail))


def add_flow(task: Task, action: str, before: str = "", after: str = "") -> None:
    if current_user.is_authenticated:
        operator_id = current_user.id
        operator_name = current_user.display_name
    else:
        operator_id = None
        operator_name = "第三方/系统"
    db.session.add(StoreFlowRecord(
        task_id=task.id,
        operator_id=operator_id,
        operator_name=operator_name,
        action=action,
        before_text=before or "",
        after_text=after or "",
    ))


def save_upload(field_name: str, subdir: str = "general") -> str:
    file = request.files.get(field_name)
    if not file or not file.filename:
        return ""
    filename = secure_filename(file.filename)
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in ALLOWED_UPLOAD_EXTENSIONS:
        flash(f"❌ 不支持的附件格式：{ext}", "danger")
        return ""
    folder = os.path.join(app.config["UPLOAD_FOLDER"], subdir)
    os.makedirs(folder, exist_ok=True)
    stored_name = f"{utc_now().strftime('%Y%m%d%H%M%S')}_{secrets.token_hex(5)}_{filename}"
    file.save(os.path.join(folder, stored_name))
    return f"{subdir}/{stored_name}"


def confirmation_rate_limit_exceeded(ip: str) -> bool:
    now_ts = utc_now().timestamp()
    history = CONFIRM_RATE_LIMIT.get(ip, [])
    history = [ts for ts in history if now_ts - ts <= CONFIRM_RATE_WINDOW_SECONDS]
    if len(history) >= CONFIRM_RATE_MAX_POSTS:
        CONFIRM_RATE_LIMIT[ip] = history
        return True
    history.append(now_ts)
    CONFIRM_RATE_LIMIT[ip] = history
    return False


def export_csv(filename: str, headers: list, rows: list) -> Response:
    output = io.StringIO()
    output.write("\ufeff")
    writer = csv.writer(output)
    writer.writerow(headers)
    writer.writerows(rows)
    return Response(
        output.getvalue(),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


def apply_task_filters(q):
    keyword = request.args.get("q", "").strip()
    status = request.args.get("status", "").strip()
    urgency = request.args.get("urgency", "").strip()
    date_from = request.args.get("date_from", "").strip()
    date_to = request.args.get("date_to", "").strip()
    min_price = request.args.get("min_price", "").strip()
    max_price = request.args.get("max_price", "").strip()
    if keyword:
        like = f"%{keyword}%"
        q = q.filter(or_(Task.code.like(like), Task.store_name.like(like), Task.address.like(like), Task.region.like(like)))
    if status:
        q = q.filter(Task.task_status == status)
    if urgency:
        q = q.filter(Task.urgency == urgency)
    if date_from:
        q = q.filter(Task.start_time >= parse_date(date_from))
    if date_to:
        q = q.filter(Task.end_time <= parse_date(date_to))
    if min_price:
        q = q.filter((Task.payment_base_price + Task.approved_extra_price) >= parse_float(min_price))
    if max_price:
        q = q.filter((Task.payment_base_price + Task.approved_extra_price) <= parse_float(max_price))
    return q


def calculate_monthly_completion(month_str: str):
    """返回当前权限范围下的运营月度完成率。月度应完成数取 Employee.monthly_target。"""
    if not month_str:
        month_str = date.today().strftime("%Y-%m")
    year, month = [int(x) for x in month_str.split("-")]
    start = date(year, month, 1)
    if month == 12:
        end = date(year + 1, 1, 1)
    else:
        end = date(year, month + 1, 1)

    operators = allowed_operators_for_current_user()
    if current_user.role == "super_admin":
        operators = Employee.query.join(User, User.employee_id == Employee.id).filter(User.role == "operator").order_by(Employee.name.asc()).all()
    rows = []
    for emp in operators:
        q = Task.query.filter(Task.operator_id == emp.id, Task.end_time >= start, Task.end_time < end)
        assigned = q.count()
        completed = q.filter(Task.task_status == "已完成").count()
        target = emp.monthly_target if emp.monthly_target is not None else 0
        rate = round(completed / target * 100, 2) if target else 0.0
        rows.append({"employee": emp, "assigned": assigned, "target": target, "completed": completed, "rate": rate})
    return rows


@app.context_processor
def inject_globals():
    return {
        "ROLE_LABELS": ROLE_LABELS,
        "TASK_STATUSES": TASK_STATUSES,
        "CONFIRMATION_STATUSES": CONFIRMATION_STATUSES,
        "CONFIRMATION_REVIEW_STATUSES": CONFIRMATION_REVIEW_STATUSES,
        "PRICE_STATUS": PRICE_STATUS,
        "TRAVEL_STATUS": TRAVEL_STATUS,
        "REJECT_CATEGORIES": REJECT_CATEGORIES,
        "date": date,
    }


# ============================================================
# 初始化数据
# ============================================================
def seed_data():
    if User.query.first():
        return

    admin_emp = Employee(name="张强", phone="13800000001", position="超管", monthly_target=0)
    supervisor_emp = Employee(name="李主管", phone="13800000002", position="业务主管", monthly_target=20)
    operator_emp = Employee(name="王运营", phone="13800000003", position="业务运营", supervisor=supervisor_emp, monthly_target=30)
    db.session.add_all([admin_emp, supervisor_emp, operator_emp])
    db.session.flush()

    admin = User(username="admin", role="super_admin", employee=admin_emp, is_active=True)
    admin.set_password("admin123")
    supervisor = User(username="supervisor", role="supervisor", employee=supervisor_emp, is_active=True)
    supervisor.set_password("supervisor123")
    operator = User(username="operator", role="operator", employee=operator_emp, is_active=True)
    operator.set_password("operator123")
    db.session.add_all([admin, supervisor, operator])

    p1 = Project(project_name="标准暗访项目", description="默认演示项目，用于门店暗访、确认、审核和打款演示。")
    db.session.add(p1)
    db.session.flush()

    task = Task(
        code=f"XF{utc_now().strftime('%Y%m%d%H%M%S')}",
        project_id=p1.id,
        creator_id=admin.id,
        supervisor_id=supervisor_emp.id,
        operator_id=operator_emp.id,
        store_name="先锋样例门店",
        region="华东一区",
        address="上海市示例路 100 号",
        urgency="一般",
        start_time=date.today(),
        end_time=date.today(),
        payment_base_price=50,
        approved_extra_price=0,
        agency_price=80,
        task_sop_html="<p><strong>标准 SOP：</strong>到店后观察门头、服务流程、APP 提交流程，并上传截图。</p>",
        task_status="进行中",
        audit_status="待审核",
        payment_status="待打款",
        confirmation_token=secrets.token_urlsafe(24),
    )
    db.session.add(task)
    db.session.flush()
    db.session.add(StoreFlowRecord(task_id=task.id, operator_id=admin.id, operator_name="系统", action="系统初始化", after_text="创建演示任务"))

    for category, content in [
        ("任务审核", "截图不清晰，请补充可识别门店信息的材料"),
        ("价格调整", "加价原因不充分，请补充说明"),
        ("路费补贴", "凭证不完整，请重新上传"),
    ]:
        db.session.add(RejectReason(user=supervisor, category=category, content=content))
        db.session.add(RejectReason(user=admin, category=category, content=content))

    db.session.commit()


def ensure_sqlite_columns():
    """轻量迁移：如果用户从旧版 zip 直接覆盖代码，SQLite 旧库也能补齐新增字段。"""
    if not app.config["SQLALCHEMY_DATABASE_URI"].startswith("sqlite"):
        return
    existing = {row[1] for row in db.session.execute(db.text("PRAGMA table_info(task)")).fetchall()}
    ddl = []
    if "confirmation_started_at" not in existing:
        ddl.append("ALTER TABLE task ADD COLUMN confirmation_started_at DATETIME")
    if "confirmation_sent_at" not in existing:
        ddl.append("ALTER TABLE task ADD COLUMN confirmation_sent_at DATETIME")
    if "confirmation_sent_to" not in existing:
        ddl.append("ALTER TABLE task ADD COLUMN confirmation_sent_to VARCHAR(128) DEFAULT ''")
    if "confirmation_sent_note" not in existing:
        ddl.append("ALTER TABLE task ADD COLUMN confirmation_sent_note TEXT DEFAULT ''")
    if "confirmation_review_status" not in existing:
        ddl.append("ALTER TABLE task ADD COLUMN confirmation_review_status VARCHAR(32) DEFAULT '未发起'")
    if "confirmation_review_note" not in existing:
        ddl.append("ALTER TABLE task ADD COLUMN confirmation_review_note TEXT DEFAULT ''")
    if "confirmation_reviewed_by" not in existing:
        ddl.append("ALTER TABLE task ADD COLUMN confirmation_reviewed_by INTEGER")
    if "confirmation_reviewed_at" not in existing:
        ddl.append("ALTER TABLE task ADD COLUMN confirmation_reviewed_at DATETIME")
    for sql in ddl:
        db.session.execute(db.text(sql))
    if ddl:
        db.session.commit()


@app.before_request
def init_db_once():
    # 让下载后的文件夹无需手动初始化即可运行。
    db.create_all()
    ensure_sqlite_columns()
    seed_data()


# ============================================================
# 账号登录
# ============================================================
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = User.query.filter_by(username=username).first()
        if not user or not user.check_password(password):
            flash("❌ 账号或密码错误", "danger")
            return redirect(url_for("login"))
        if not user.is_active:
            flash("❌ 账号已被禁用，请联系超级管理员", "danger")
            return redirect(url_for("login"))
        login_user(user)
        log_operation("系统安全", "登录", f"账号 {user.username} 登录系统")
        db.session.commit()
        return redirect(url_for("dashboard"))
    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    log_operation("系统安全", "登出", f"账号 {current_user.username} 退出系统")
    db.session.commit()
    logout_user()
    return redirect(url_for("login"))


# ============================================================
# 工作台
# ============================================================
@app.route("/")
@login_required
def dashboard():
    tasks = visible_tasks_query().order_by(Task.created_at.desc()).all()
    total = len(tasks)
    completed = len([t for t in tasks if t.task_status == "已完成"])
    overdue = len([t for t in tasks if t.is_overdue])
    pending_audit = len([t for t in tasks if t.task_status == "待主管审核"])
    price_pending = 0
    travel_pending = 0
    for t in tasks:
        price_pending += len([p for p in t.price_adjustments if p.status in ["待主管审批", "主管已通过待超管审批"]])
        travel_pending += len([s for s in t.travel_subsidies if s.status in ["待主管审批", "主管已通过待超管审批"]])

    total_payment = round(sum(t.final_payment_price for t in tasks), 2)
    agency_total = round(sum((t.agency_price or 0) for t in tasks), 2) if current_user.role == "super_admin" else None
    recent_flows = StoreFlowRecord.query.join(Task).filter(Task.id.in_([t.id for t in tasks] or [0])).order_by(StoreFlowRecord.created_at.desc()).limit(12).all()

    stats = {
        "total": total,
        "completed": completed,
        "overdue": overdue,
        "pending_audit": pending_audit,
        "price_pending": price_pending,
        "travel_pending": travel_pending,
        "total_payment": total_payment,
        "agency_total": agency_total,
    }
    return render_template("dashboard.html", stats=stats, tasks=tasks[:8], recent_flows=recent_flows)


# ============================================================
# 任务列表、新建、详情、编辑、分配
# ============================================================
@app.route("/tasks")
@login_required
def tasks():
    q = apply_task_filters(visible_tasks_query())
    tasks_list = q.order_by(Task.created_at.desc()).all()
    projects = Project.query.order_by(Project.project_name.asc()).all()
    supervisors = allowed_supervisors_for_current_user()
    operators = allowed_operators_for_current_user()
    return render_template("tasks.html", tasks=tasks_list, projects=projects, supervisors=supervisors, operators=operators)


@app.route("/tasks/create", methods=["POST"])
@login_required
@role_required("super_admin", "supervisor")
def create_task():
    project_id = request.form.get("project_id")
    store_name = request.form.get("store_name", "").strip()
    if not project_id or not store_name:
        flash("❌ 项目和门店名称不能为空", "danger")
        return redirect(url_for("tasks"))

    supervisor_id = request.form.get("supervisor_id") or None
    operator_id = request.form.get("operator_id") or None

    if current_user.role == "supervisor":
        supervisor_id = current_employee_id()
    elif current_user.role == "operator":
        operator_id = current_employee_id()
        supervisor_id = current_user.employee.supervisor_id if current_user.employee else None

    start_time, end_time, date_error = dates_are_valid(request.form.get("start_time"), request.form.get("end_time"))
    if date_error:
        flash(f"❌ {date_error}", "danger")
        return redirect(url_for("tasks"))

    task = Task(
        code=f"XF{utc_now().strftime('%Y%m%d%H%M%S')}{secrets.token_hex(2).upper()}",
        project_id=int(project_id),
        creator_id=current_user.id,
        supervisor_id=int(supervisor_id) if supervisor_id else None,
        operator_id=int(operator_id) if operator_id else None,
        store_name=store_name,
        region=request.form.get("region", "未分区").strip() or "未分区",
        address=request.form.get("address", "").strip(),
        urgency=request.form.get("urgency", "一般"),
        start_time=start_time,
        end_time=end_time,
        payment_base_price=parse_float(request.form.get("payment_base_price"), 0),
        agency_price=parse_float(request.form.get("agency_price"), 0) if current_user.role == "super_admin" and request.form.get("agency_price") else None,
        task_sop_html=request.form.get("task_sop_html", ""),
        store_remarks=request.form.get("store_remarks", ""),
        task_status="待运营承接" if operator_id else "待主管承接",
        confirmation_token=secrets.token_urlsafe(24),
    )
    if current_user.role == "operator":
        task.task_status = "待主管审核"
        task.audit_status = "待主管审核"
    db.session.add(task)
    db.session.flush()
    add_flow(task, "新建门店任务", after=f"门店：{task.store_name}；项目：{task.project.project_name}；状态：{task.task_status}")
    log_operation("任务管理", "新建任务", f"新建 {task.code} - {task.store_name}")
    db.session.commit()
    flash("✅ 门店任务已创建", "success")
    return redirect(url_for("task_detail", task_id=task.id))


@app.route("/tasks/<int:task_id>")
@login_required
def task_detail(task_id):
    task = Task.query.get_or_404(task_id)
    if not can_access_task(task):
        abort(403)
    projects = Project.query.order_by(Project.project_name.asc()).all()
    supervisors = allowed_supervisors_for_current_user()
    operators = allowed_operators_for_current_user()
    reject_reasons = RejectReason.query.filter_by(user_id=current_user.id).order_by(RejectReason.created_at.desc()).all()
    return render_template(
        "task_detail.html",
        task=task,
        projects=projects,
        supervisors=supervisors,
        operators=operators,
        reject_reasons=reject_reasons,
    )


@app.route("/tasks/<int:task_id>/basic", methods=["POST"])
@login_required
def update_task_basic(task_id):
    task = Task.query.get_or_404(task_id)
    if not can_manage_task(task):
        abort(403)
    before = f"{task.store_name} / {task.address} / {task.start_time}~{task.end_time} / 基准价{task.payment_base_price}"
    task.project_id = int(request.form.get("project_id", task.project_id))
    task.store_name = request.form.get("store_name", task.store_name).strip()
    task.region = request.form.get("region", task.region).strip()
    task.address = request.form.get("address", task.address).strip()
    task.urgency = request.form.get("urgency", task.urgency)
    new_start, new_end, date_error = dates_are_valid(request.form.get("start_time"), request.form.get("end_time"))
    if date_error:
        flash(f"❌ {date_error}", "danger")
        return redirect(url_for("task_detail", task_id=task.id))
    task.start_time = new_start
    task.end_time = new_end
    if current_user.role in ["super_admin", "supervisor"]:
        task.payment_base_price = parse_float(request.form.get("payment_base_price"), task.payment_base_price)
    if current_user.role == "super_admin":
        task.agency_price = parse_float(request.form.get("agency_price"), task.agency_price or 0)
    task.store_remarks = request.form.get("store_remarks", task.store_remarks)
    after = f"{task.store_name} / {task.address} / {task.start_time}~{task.end_time} / 基准价{task.payment_base_price}"
    add_flow(task, "编辑门店基础信息", before, after)
    log_operation("任务管理", "编辑基础信息", task.code)
    db.session.commit()
    flash("✅ 基础信息已保存", "success")
    return redirect(url_for("task_detail", task_id=task.id))


@app.route("/tasks/<int:task_id>/assign", methods=["POST"])
@login_required
@role_required("super_admin", "supervisor")
def assign_task(task_id):
    task = Task.query.get_or_404(task_id)
    if not can_access_task(task):
        abort(403)
    before = f"主管：{task.supervisor.name if task.supervisor else '未分配'}；运营：{task.operator.name if task.operator else '未分配'}"
    if current_user.role == "super_admin":
        task.supervisor_id = int(request.form.get("supervisor_id") or task.supervisor_id or 0) or None
    if request.form.get("operator_id"):
        op = Employee.query.get(int(request.form.get("operator_id")))
        if current_user.role == "supervisor" and op.supervisor_id != current_employee_id():
            abort(403)
        task.operator_id = op.id
        task.task_status = "待运营承接"
    if request.form.get("task_status") in TASK_STATUSES:
        task.task_status = request.form.get("task_status")
    after = f"主管：{task.supervisor.name if task.supervisor else '未分配'}；运营：{task.operator.name if task.operator else '未分配'}；状态：{task.task_status}"
    add_flow(task, "任务分配/状态调整", before, after)
    log_operation("任务管理", "任务分配", f"{task.code} {after}")
    db.session.commit()
    flash("✅ 分配信息已更新", "success")
    return redirect(url_for("task_detail", task_id=task.id))


@app.route("/tasks/<int:task_id>/accept", methods=["POST"])
@login_required
def accept_task(task_id):
    task = Task.query.get_or_404(task_id)
    if not can_access_task(task):
        abort(403)

    # 状态机前置拦截：已完结任务不可重新承接，避免“已完成/放弃执行”被误点倒退。
    if task.task_status in ["已完成", "放弃执行"]:
        flash("❌ 任务已完结，无法再次承接", "danger")
        return redirect(url_for("task_detail", task_id=task.id))

    before = task.task_status
    if current_user.role == "supervisor" and task.supervisor_id == current_employee_id():
        if task.task_status != "待主管承接":
            flash("❌ 当前任务状态不允许主管承接", "danger")
            return redirect(url_for("task_detail", task_id=task.id))
        task.task_status = "待运营承接" if task.operator_id else "待主管分配"
    elif current_user.role == "operator" and task.operator_id == current_employee_id():
        if task.task_status != "待运营承接":
            flash("❌ 当前任务状态不允许运营承接", "danger")
            return redirect(url_for("task_detail", task_id=task.id))
        task.task_status = "进行中"
    else:
        abort(403)
    add_flow(task, "任务承接确认", before, task.task_status)
    db.session.commit()
    flash("✅ 已确认承接", "success")
    return redirect(url_for("task_detail", task_id=task.id))


@app.route("/tasks/<int:task_id>/sop", methods=["POST"])
@login_required
@role_required("super_admin", "supervisor")
def update_sop(task_id):
    task = Task.query.get_or_404(task_id)
    if not can_access_task(task):
        abort(403)
    before = task.task_sop_html[:500]
    sop_html = request.form.get("task_sop_html", "")
    media_path = save_upload("sop_media", "sop_media")
    if media_path:
        lower = media_path.lower()
        if lower.endswith((".mp4", ".mov")):
            sop_html += f'<p><video controls style="max-width:100%;border-radius:12px" src="{url_for("uploaded_file", filename=media_path)}"></video></p>'
        else:
            sop_html += f'<p><img style="max-width:100%;border-radius:12px" src="{url_for("uploaded_file", filename=media_path)}" alt="SOP附件"></p>'
    task.task_sop_html = sop_html
    add_flow(task, "SOP 富文本编辑", before, task.task_sop_html[:500])
    log_operation("SOP", "编辑", task.code)
    db.session.commit()
    flash("✅ SOP 已保存", "success")
    return redirect(url_for("task_detail", task_id=task.id))


@app.route("/tasks/<int:task_id>/executor", methods=["POST"])
@login_required
def update_executor_payee(task_id):
    task = Task.query.get_or_404(task_id)
    if not can_manage_task(task):
        abort(403)
    before = f"执行人：{task.executor_name}/{task.executor_phone}；收款人：{task.payee_name}/{task.payee_phone}/{task.payee_bank}/{task.payee_account}"
    task.executor_name = request.form.get("executor_name", "").strip()
    task.executor_phone = request.form.get("executor_phone", "").strip()
    task.payee_name = request.form.get("payee_name", "").strip()
    task.payee_phone = request.form.get("payee_phone", "").strip()
    task.payee_bank = request.form.get("payee_bank", "").strip()
    task.payee_account = request.form.get("payee_account", "").strip()
    task.executor_remarks = request.form.get("executor_remarks", "").strip()
    after = f"执行人：{task.executor_name}/{task.executor_phone}；收款人：{task.payee_name}/{task.payee_phone}/{task.payee_bank}/{task.payee_account}"
    add_flow(task, "编辑执行人/收款人信息", before, after)
    db.session.commit()
    flash("✅ 执行人和收款信息已保存", "success")
    return redirect(url_for("task_detail", task_id=task.id))


@app.route("/tasks/<int:task_id>/payment_status", methods=["POST"])
@login_required
@role_required("super_admin", "supervisor")
def update_payment_status(task_id):
    task = Task.query.get_or_404(task_id)
    if not can_access_task(task):
        abort(403)
    before = task.payment_status
    task.payment_status = request.form.get("payment_status", task.payment_status)
    add_flow(task, "打款状态更新", before, task.payment_status)
    db.session.commit()
    flash("✅ 打款状态已更新", "success")
    return redirect(url_for("task_detail", task_id=task.id))


# ============================================================
# 神秘顾客、结果提交、审核
# ============================================================
@app.route("/tasks/<int:task_id>/shopper", methods=["POST"])
@login_required
def add_shopper(task_id):
    task = Task.query.get_or_404(task_id)
    if not can_manage_task(task):
        abort(403)
    shopper = MysteryShopper(
        task_id=task.id,
        created_by=current_user.id,
        name=request.form.get("name", "").strip(),
        phone=request.form.get("phone", "").strip(),
        identity_note=request.form.get("identity_note", "").strip(),
        status=request.form.get("status", "待执行"),
    )
    if not shopper.name:
        flash("❌ 神秘顾客姓名不能为空", "danger")
        return redirect(url_for("task_detail", task_id=task.id))
    db.session.add(shopper)
    add_flow(task, "新增神秘顾客", after=f"{shopper.name}/{shopper.phone}/{shopper.status}")
    db.session.commit()
    flash("✅ 神秘顾客已添加", "success")
    return redirect(url_for("task_detail", task_id=task.id))


@app.route("/shopper/<int:shopper_id>/update", methods=["POST"])
@login_required
def update_shopper(shopper_id):
    shopper = MysteryShopper.query.get_or_404(shopper_id)
    task = shopper.task
    if not can_manage_task(task):
        abort(403)
    before = f"{shopper.name}/{shopper.phone}/{shopper.status}/{shopper.identity_note}"
    shopper.name = request.form.get("name", shopper.name).strip()
    shopper.phone = request.form.get("phone", shopper.phone).strip()
    shopper.identity_note = request.form.get("identity_note", shopper.identity_note).strip()
    shopper.status = request.form.get("status", shopper.status)
    add_flow(task, "编辑神秘顾客", before, f"{shopper.name}/{shopper.phone}/{shopper.status}/{shopper.identity_note}")
    db.session.commit()
    flash("✅ 神秘顾客档案已更新", "success")
    return redirect(url_for("task_detail", task_id=task.id))


@app.route("/shopper/<int:shopper_id>/delete", methods=["POST"])
@login_required
def delete_shopper(shopper_id):
    shopper = MysteryShopper.query.get_or_404(shopper_id)
    task = shopper.task
    if not can_manage_task(task):
        abort(403)
    detail = f"{shopper.name}/{shopper.phone}/{shopper.status}"
    db.session.delete(shopper)
    add_flow(task, "删除神秘顾客", before=detail, after="已删除")
    db.session.commit()
    flash("✅ 神秘顾客档案已删除", "success")
    return redirect(url_for("task_detail", task_id=task.id))


@app.route("/tasks/<int:task_id>/abandon", methods=["POST"])
@login_required
def abandon_task(task_id):
    task = Task.query.get_or_404(task_id)
    if not can_manage_task(task):
        abort(403)
    reason = request.form.get("reason", "").strip()
    before = task.task_status
    task.task_status = "放弃执行"
    task.exception_summary = reason
    add_flow(task, "放弃执行", before, f"放弃执行；原因：{reason}")
    db.session.commit()
    flash("✅ 已记录放弃执行状态", "success")
    return redirect(url_for("task_detail", task_id=task.id))


@app.route("/tasks/<int:task_id>/result", methods=["POST"])
@login_required
def submit_result(task_id):
    task = Task.query.get_or_404(task_id)
    if not can_manage_task(task):
        abort(403)

    # 状态机前置拦截：防止重复提交把已完成任务回退到“待主管审核”。
    if task.task_status in ["已完成", "放弃执行"]:
        flash("❌ 任务已完结，无法再次提交结果", "danger")
        return redirect(url_for("task_detail", task_id=task.id))
    if task.task_status not in ["待运营承接", "进行中", "已退回"]:
        flash("❌ 当前任务状态不允许提交结果", "danger")
        return redirect(url_for("task_detail", task_id=task.id))

    file_path = save_upload("screenshot", "task_results")
    result = TaskResult(
        task_id=task.id,
        submitted_by=current_user.id,
        problem_list=request.form.get("problem_list", ""),
        result_description=request.form.get("result_description", ""),
        screenshot_path=file_path,
        status="待审核",
    )
    db.session.add(result)
    before = task.task_status
    task.task_status = "待主管审核"
    task.audit_status = "待审核"
    add_flow(task, "提交任务结果", before, f"待主管审核；问题清单：{result.problem_list[:120]}")
    db.session.commit()
    flash("✅ 任务结果已提交，等待主管审核", "success")
    return redirect(url_for("task_detail", task_id=task.id))


@app.route("/results/<int:result_id>/review", methods=["POST"])
@login_required
@role_required("super_admin", "supervisor")
def review_result(result_id):
    result = TaskResult.query.get_or_404(result_id)
    task = result.task
    if not can_access_task(task):
        abort(403)
    decision = request.form.get("decision")
    comment = request.form.get("review_comment", "")
    before = f"结果状态：{result.status}；任务状态：{task.task_status}"
    result.review_comment = comment
    result.reviewed_by = current_user.id
    result.reviewed_at = utc_now()
    if decision == "pass":
        result.status = "已通过"
        task.task_status = "已完成"
        task.audit_status = "已通过"
    elif decision == "return":
        result.status = "已退回"
        task.task_status = "已退回"
        task.audit_status = "已退回"
    elif decision == "abnormal":
        result.status = "异常上报"
        task.task_status = "异常上报"
        task.audit_status = "异常上报"
        task.exception_summary = comment
    else:
        flash("❌ 审核决定无效", "danger")
        return redirect(url_for("task_detail", task_id=task.id))
    add_flow(task, "任务结果审核", before, f"结果：{result.status}；任务：{task.task_status}；意见：{comment}")
    db.session.commit()
    flash("✅ 审核结果已保存", "success")
    return redirect(url_for("task_detail", task_id=task.id))


# ============================================================
# 价格调整审批链
# ============================================================
def approve_price_adjustment(pa: PriceAdjustment, comment: str, operator_level: str):
    task = pa.task
    before = f"任务价：{task.final_payment_price}；加价累计：{task.approved_extra_price}；申请状态：{pa.status}"
    if pa.amount_change <= 0:
        pa.status = "已通过"
        task.approved_extra_price += pa.amount_change
    elif pa.amount_change < 5:
        pa.status = "已自动通过"
        task.approved_extra_price += pa.amount_change
    elif 5 <= pa.amount_change <= 10:
        if operator_level in ["supervisor", "super_admin"]:
            pa.status = "已通过"
            pa.supervisor_comment = comment
            pa.supervisor_reviewed_at = utc_now()
            task.approved_extra_price += pa.amount_change
    else:
        # >10 元必须超管终审。主管只能提交给超管；超管可直接终审通过。
        if operator_level == "supervisor":
            pa.status = "主管已通过待超管审批"
            pa.supervisor_comment = comment
            pa.supervisor_reviewed_at = utc_now()
        elif operator_level == "super_admin":
            pa.status = "已通过"
            pa.admin_comment = comment
            pa.admin_reviewed_at = utc_now()
            task.approved_extra_price += pa.amount_change
    after = f"任务价：{task.final_payment_price}；加价累计：{task.approved_extra_price}；申请状态：{pa.status}"
    add_flow(task, "价格调整审批", before, after)


@app.route("/tasks/<int:task_id>/price", methods=["POST"])
@login_required
def create_price_adjustment(task_id):
    task = Task.query.get_or_404(task_id)
    if not can_manage_task(task):
        abort(403)
    amount = parse_float(request.form.get("amount_change"), 0)
    reason = request.form.get("reason", "").strip()
    if amount == 0:
        flash("❌ 调整金额不能为 0", "danger")
        return redirect(url_for("task_detail", task_id=task.id))
    pa = PriceAdjustment(task_id=task.id, applicant_id=current_user.id, amount_change=amount, reason=reason)
    db.session.add(pa)
    db.session.flush()
    if amount <= 0 or amount < 5:
        approve_price_adjustment(pa, "系统规则自动通过", current_user.role)
    elif 5 <= amount <= 10 and current_user.role in ["supervisor", "super_admin"]:
        approve_price_adjustment(pa, "提交人具备主管/超管权限，直接通过", current_user.role)
    elif amount > 10 and current_user.role == "super_admin":
        approve_price_adjustment(pa, "超管直接终审", current_user.role)
    else:
        pa.status = "待主管审批"
        add_flow(task, "提交价格调整申请", after=f"金额：{amount}；原因：{reason}；状态：{pa.status}")
    db.session.commit()
    flash("✅ 价格调整申请已提交/处理", "success")
    return redirect(url_for("task_detail", task_id=task.id))


@app.route("/price/<int:pa_id>/review", methods=["POST"])
@login_required
@role_required("super_admin", "supervisor")
def review_price(pa_id):
    pa = PriceAdjustment.query.get_or_404(pa_id)
    task = pa.task
    if not can_access_task(task):
        abort(403)
    decision = request.form.get("decision")
    comment = request.form.get("comment", "")
    before = pa.status
    if decision == "reject":
        pa.status = "已驳回"
        if current_user.role == "super_admin":
            pa.admin_comment = comment
            pa.admin_reviewed_at = utc_now()
        else:
            pa.supervisor_comment = comment
            pa.supervisor_reviewed_at = utc_now()
        add_flow(task, "驳回价格调整", before, f"已驳回；理由：{comment}")
    elif decision == "approve":
        if pa.status == "待主管审批" and current_user.role in ["supervisor", "super_admin"]:
            approve_price_adjustment(pa, comment, "supervisor" if current_user.role == "supervisor" else "super_admin")
        elif pa.status == "主管已通过待超管审批" and current_user.role == "super_admin":
            approve_price_adjustment(pa, comment, "super_admin")
        else:
            flash("❌ 当前审批节点与账号权限不匹配", "danger")
            return redirect(url_for("task_detail", task_id=task.id))
    else:
        flash("❌ 审批决定无效", "danger")
        return redirect(url_for("task_detail", task_id=task.id))
    db.session.commit()
    flash("✅ 价格审批已处理", "success")
    return redirect(url_for("task_detail", task_id=task.id))


# ============================================================
# 路费补贴审批链
# ============================================================
@app.route("/tasks/<int:task_id>/travel", methods=["POST"])
@login_required
def create_travel(task_id):
    task = Task.query.get_or_404(task_id)
    if not can_manage_task(task):
        abort(403)
    amount = parse_float(request.form.get("amount"), 0)
    if amount <= 0:
        flash("❌ 路费补贴金额必须大于 0", "danger")
        return redirect(url_for("task_detail", task_id=task.id))
    voucher_path = save_upload("voucher", "travel_vouchers")
    subsidy = TravelSubsidy(
        task_id=task.id,
        applicant_id=current_user.id,
        amount=amount,
        reason=request.form.get("reason", ""),
        voucher_path=voucher_path,
        status="待主管审批",
    )
    db.session.add(subsidy)
    add_flow(task, "提交路费补贴申请", after=f"金额：{amount}；原因：{subsidy.reason}")
    db.session.commit()
    flash("✅ 路费补贴申请已提交", "success")
    return redirect(url_for("task_detail", task_id=task.id))


@app.route("/travel/<int:subsidy_id>/review", methods=["POST"])
@login_required
@role_required("super_admin", "supervisor")
def review_travel(subsidy_id):
    subsidy = TravelSubsidy.query.get_or_404(subsidy_id)
    task = subsidy.task
    if not can_access_task(task):
        abort(403)
    decision = request.form.get("decision")
    comment = request.form.get("comment", "")
    before = subsidy.status
    if decision == "reject":
        subsidy.status = "已驳回"
        if current_user.role == "super_admin":
            subsidy.admin_comment = comment
            subsidy.admin_reviewed_at = utc_now()
        else:
            subsidy.supervisor_comment = comment
            subsidy.supervisor_reviewed_at = utc_now()
        add_flow(task, "驳回路费补贴", before, f"已驳回；理由：{comment}")
    elif decision == "approve":
        if subsidy.amount <= 15:
            if current_user.role not in ["supervisor", "super_admin"]:
                abort(403)
            subsidy.status = "已通过"
            subsidy.supervisor_comment = comment
            subsidy.supervisor_reviewed_at = utc_now()
        else:
            if subsidy.status == "待主管审批" and current_user.role == "supervisor":
                subsidy.status = "主管已通过待超管审批"
                subsidy.supervisor_comment = comment
                subsidy.supervisor_reviewed_at = utc_now()
            elif current_user.role == "super_admin":
                subsidy.status = "已通过"
                subsidy.admin_comment = comment
                subsidy.admin_reviewed_at = utc_now()
            else:
                flash("❌ 15 元以上补贴必须由超管终审", "danger")
                return redirect(url_for("task_detail", task_id=task.id))
        add_flow(task, "路费补贴审批", before, f"{subsidy.status}；意见：{comment}")
    else:
        flash("❌ 审批决定无效", "danger")
        return redirect(url_for("task_detail", task_id=task.id))
    db.session.commit()
    flash("✅ 路费补贴审批已处理", "success")
    return redirect(url_for("task_detail", task_id=task.id))


# ============================================================
# 第三方门店执行确认链接
# ============================================================
@app.route("/tasks/<int:task_id>/confirmation/start", methods=["POST"])
@login_required
def start_confirmation(task_id):
    task = Task.query.get_or_404(task_id)
    if not can_access_task(task):
        abort(403)
    before = f"状态：{task.confirmation_status}；核对：{task.confirmation_review_status}"
    if not task.confirmation_token:
        task.confirmation_token = secrets.token_urlsafe(32)
    task.confirmation_started_at = utc_now()
    if task.confirmation_review_status in [None, "", "未发起", "链接已作废"]:
        task.confirmation_review_status = "待第三方提交"
    add_flow(task, "发起门店执行确认", before, f"已发起；确认链接：/confirm/{task.confirmation_token[:8]}...")
    db.session.commit()
    flash("✅ 已发起门店执行确认，可复制链接发送给神秘顾客", "success")
    return redirect(url_for("task_detail", task_id=task.id))


@app.route("/tasks/<int:task_id>/confirmation/sent", methods=["POST"])
@login_required
def mark_confirmation_sent(task_id):
    task = Task.query.get_or_404(task_id)
    if not can_access_task(task):
        abort(403)
    if not task.confirmation_started_at:
        task.confirmation_started_at = utc_now()
    task.confirmation_sent_at = utc_now()
    task.confirmation_sent_to = request.form.get("sent_to", "").strip()
    task.confirmation_sent_note = request.form.get("sent_note", "").strip()
    if task.confirmation_review_status in [None, "", "未发起"]:
        task.confirmation_review_status = "待第三方提交"
    add_flow(task, "标记确认链接已发送", after=f"发送对象：{task.confirmation_sent_to or '未填写'}；备注：{task.confirmation_sent_note}")
    db.session.commit()
    flash("✅ 已记录确认链接发送信息", "success")
    return redirect(url_for("task_detail", task_id=task.id))


@app.route("/tasks/<int:task_id>/confirmation/regenerate", methods=["POST"])
@login_required
@role_required("super_admin", "supervisor")
def regenerate_confirmation(task_id):
    task = Task.query.get_or_404(task_id)
    if not can_access_task(task):
        abort(403)
    before = f"旧状态：{task.confirmation_status}；旧 token 尾号：{(task.confirmation_token or '')[-8:]}"
    task.confirmation_token = secrets.token_urlsafe(32)
    task.confirmation_status = "未确认"
    task.confirmation_note = ""
    task.confirmation_screenshot = ""
    task.confirmation_submitted_at = None
    task.confirmation_started_at = utc_now()
    task.confirmation_sent_at = None
    task.confirmation_sent_to = ""
    task.confirmation_sent_note = ""
    task.confirmation_review_status = "待第三方提交"
    task.confirmation_review_note = ""
    task.confirmation_reviewed_by = None
    task.confirmation_reviewed_at = None
    add_flow(task, "重新生成确认链接", before, f"新 token 尾号：{task.confirmation_token[-8:]}；旧链接已作废")
    db.session.commit()
    flash("✅ 已重新生成确认链接，旧链接已无法继续访问", "success")
    return redirect(url_for("task_detail", task_id=task.id))


@app.route("/tasks/<int:task_id>/confirmation/review", methods=["POST"])
@login_required
@role_required("super_admin", "supervisor")
def review_confirmation(task_id):
    task = Task.query.get_or_404(task_id)
    if not can_access_task(task):
        abort(403)
    decision = request.form.get("decision")
    note = request.form.get("review_note", "").strip()
    if not task.confirmation_submitted_at:
        flash("❌ 第三方尚未提交，不能核对截图", "danger")
        return redirect(url_for("task_detail", task_id=task.id))
    if task.confirmation_status != "已执行已提交" or not task.confirmation_screenshot:
        flash("❌ 只有“已执行已提交”且已上传截图的记录才需要截图核对", "danger")
        return redirect(url_for("task_detail", task_id=task.id))
    before = task.confirmation_review_status
    if decision == "pass":
        task.confirmation_review_status = "截图核对通过"
    elif decision == "reject":
        task.confirmation_review_status = "截图核对驳回"
    else:
        flash("❌ 核对决定无效", "danger")
        return redirect(url_for("task_detail", task_id=task.id))
    task.confirmation_review_note = note
    task.confirmation_reviewed_by = current_user.id
    task.confirmation_reviewed_at = utc_now()
    add_flow(task, "门店确认截图核对", before, f"{task.confirmation_review_status}；意见：{note}")
    db.session.commit()
    flash("✅ 门店确认截图核对结果已保存", "success")
    return redirect(url_for("task_detail", task_id=task.id))


@app.route("/confirm/<token>", methods=["GET", "POST"])
def confirm_execution(token):
    db.create_all()
    ensure_sqlite_columns()
    seed_data()
    task = Task.query.filter_by(confirmation_token=token).first_or_404()
    if request.method == "POST":
        ip = request.remote_addr or "unknown"
        if confirmation_rate_limit_exceeded(ip):
            flash("提交过于频繁，请稍后再试。", "danger")
            return redirect(url_for("confirm_execution", token=token))
        if task.confirmation_submitted_at:
            flash("该确认链接已提交，不能重复修改。", "warning")
            return redirect(url_for("confirm_execution", token=token))
        status = request.form.get("confirmation_status")
        if status not in CONFIRMATION_STATUSES or status == "未确认":
            flash("请选择正确的执行状态", "danger")
            return redirect(url_for("confirm_execution", token=token))
        screenshot_path = save_upload("confirmation_screenshot", "confirmations")
        if status == "已执行已提交" and not screenshot_path:
            flash("选择“已执行已提交”时，必须上传 APP 端报告提交成功截图。", "danger")
            return redirect(url_for("confirm_execution", token=token))
        before = f"确认：{task.confirmation_status}；核对：{task.confirmation_review_status}；任务：{task.task_status}"
        task.confirmation_status = status
        task.confirmation_note = request.form.get("confirmation_note", "")
        task.confirmation_screenshot = screenshot_path
        task.confirmation_submitted_at = utc_now()
        if status == "已执行已提交":
            task.confirmation_review_status = "待核对"
        elif status == "放弃执行":
            task.confirmation_review_status = "无需核对"
            task.task_status = "放弃执行"
        else:
            task.confirmation_review_status = "待第三方提交"
        add_flow(task, "第三方门店执行确认", before, f"{status}；说明：{task.confirmation_note}")
        db.session.commit()
        flash("提交成功。请勿重复提交，后台将根据材料进行核对。", "success")
        return redirect(url_for("confirm_execution", token=token))
    return render_template("confirm.html", task=task)


@app.route("/confirm/<token>/screenshot")
def confirm_screenshot_file(token):
    task = Task.query.filter_by(confirmation_token=token).first_or_404()
    if not task.confirmation_screenshot:
        abort(404)
    return send_from_directory(app.config["UPLOAD_FOLDER"], task.confirmation_screenshot)


# ============================================================
# 项目、人员、账号、驳回理由
# ============================================================
@app.route("/projects", methods=["GET", "POST"])
@login_required
@role_required("super_admin", "supervisor")
def projects():
    if request.method == "POST":
        p = Project(project_name=request.form.get("project_name", "").strip(), description=request.form.get("description", ""))
        if not p.project_name:
            flash("❌ 项目名称不能为空", "danger")
        else:
            db.session.add(p)
            log_operation("项目管理", "新增项目", p.project_name)
            db.session.commit()
            flash("✅ 项目已创建", "success")
        return redirect(url_for("projects"))
    return render_template("projects.html", projects=Project.query.order_by(Project.created_at.desc()).all())


@app.route("/projects/<int:project_id>/payment", methods=["POST"])
@login_required
@role_required("super_admin", "supervisor")
def update_project_payment(project_id):
    p = Project.query.get_or_404(project_id)
    before = p.payment_status
    p.payment_status = request.form.get("payment_status", p.payment_status)
    log_operation("项目管理", "更新项目打款状态", f"{p.project_name}: {before}->{p.payment_status}")
    db.session.commit()
    flash("✅ 项目打款状态已更新", "success")
    return redirect(url_for("projects"))


@app.route("/employees", methods=["GET", "POST"])
@login_required
@role_required("super_admin", "supervisor")
def employees():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        phone = request.form.get("phone", "").strip()
        monthly_target = int(request.form.get("monthly_target") or 0)
        if not name:
            flash("❌ 姓名不能为空", "danger")
            return redirect(url_for("employees"))

        if current_user.role == "supervisor":
            # 主管可新增自己的分管运营人员档案，但不能创建主管/超管档案，也不能绑定到其他主管名下。
            emp = Employee(
                name=name,
                phone=phone,
                position="运营",
                supervisor_id=current_employee_id(),
                monthly_target=monthly_target,
            )
        else:
            emp = Employee(
                name=name,
                phone=phone,
                position=request.form.get("position", "").strip(),
                supervisor_id=int(request.form.get("supervisor_id")) if request.form.get("supervisor_id") else None,
                monthly_target=monthly_target,
            )

        db.session.add(emp)
        db.session.commit()
        flash("✅ 人员档案已创建", "success")
        return redirect(url_for("employees"))

    if current_user.role == "supervisor":
        emp_id = current_employee_id()
        all_employees = Employee.query.filter(or_(Employee.id == emp_id, Employee.supervisor_id == emp_id)).order_by(Employee.created_at.desc()).all()
    else:
        all_employees = Employee.query.order_by(Employee.created_at.desc()).all()
    supervisors = Employee.query.join(User, User.employee_id == Employee.id).filter(User.role == "supervisor").all()
    return render_template("employees.html", employees=all_employees, supervisors=supervisors)


@app.route("/employees/<int:emp_id>/target", methods=["POST"])
@login_required
@role_required("super_admin", "supervisor")
def update_employee_target(emp_id):
    emp = Employee.query.get_or_404(emp_id)
    if current_user.role == "supervisor" and emp.supervisor_id != current_employee_id() and emp.id != current_employee_id():
        abort(403)
    emp.monthly_target = int(request.form.get("monthly_target", 0) or 0)
    db.session.commit()
    flash("✅ 月度目标已更新", "success")
    return redirect(url_for("employees"))


@app.route("/users", methods=["GET", "POST"])
@login_required
@role_required("super_admin")
def users():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()
        role = request.form.get("role", "operator")
        employee_id = request.form.get("employee_id") or None
        if not username or not password:
            flash("❌ 账号和密码不能为空", "danger")
            return redirect(url_for("users"))
        if User.query.filter_by(username=username).first():
            flash("❌ 账号已存在", "danger")
            return redirect(url_for("users"))
        user = User(username=username, role=role, is_active=True, employee_id=int(employee_id) if employee_id else None)
        user.set_password(password)
        db.session.add(user)
        log_operation("账号管理", "创建账号", f"{username}/{role}")
        db.session.commit()
        flash("✅ 账号已创建", "success")
        return redirect(url_for("users"))
    return render_template("users.html", users=User.query.order_by(User.created_at.desc()).all(), employees=Employee.query.order_by(Employee.name.asc()).all())


@app.route("/users/<int:user_id>/toggle", methods=["POST"])
@login_required
@role_required("super_admin")
def toggle_user(user_id):
    user = User.query.get_or_404(user_id)
    if user.id == current_user.id:
        flash("❌ 不能禁用当前登录账号", "danger")
        return redirect(url_for("users"))
    if user.role == "super_admin" and user.is_active and User.query.filter_by(role="super_admin", is_active=True).count() <= 1:
        flash("❌ 不能禁用最后一个超级管理员", "danger")
        return redirect(url_for("users"))
    user.is_active = not user.is_active
    log_operation("账号管理", "启停账号", f"{user.username}: {'启用' if user.is_active else '禁用'}")
    db.session.commit()
    flash("✅ 账号状态已更新", "success")
    return redirect(url_for("users"))


@app.route("/users/<int:user_id>/reset", methods=["POST"])
@login_required
@role_required("super_admin")
def reset_password(user_id):
    user = User.query.get_or_404(user_id)
    new_password = request.form.get("password", "").strip()
    if not new_password:
        flash("❌ 新密码不能为空", "danger")
        return redirect(url_for("users"))
    user.set_password(new_password)
    log_operation("账号管理", "重置密码", user.username)
    db.session.commit()
    flash("✅ 密码已重置", "success")
    return redirect(url_for("users"))


@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    if request.method == "POST":
        category = request.form.get("category", "其他")
        content = request.form.get("content", "").strip()
        if content:
            db.session.add(RejectReason(user_id=current_user.id, category=category, content=content))
            db.session.commit()
            flash("✅ 常用驳回理由已添加", "success")
        return redirect(url_for("settings"))
    reasons = RejectReason.query.filter_by(user_id=current_user.id).order_by(RejectReason.created_at.desc()).all()
    return render_template("settings.html", reasons=reasons)


@app.route("/settings/reasons/<int:reason_id>/delete", methods=["POST"])
@login_required
def delete_reject_reason(reason_id):
    reason = RejectReason.query.get_or_404(reason_id)
    if reason.user_id != current_user.id:
        abort(403)
    db.session.delete(reason)
    db.session.commit()
    flash("✅ 常用驳回理由已删除", "success")
    return redirect(url_for("settings"))


# ============================================================
# 批量导入导出与报表
# ============================================================
@app.route("/tasks/template")
@login_required
def task_import_template():
    headers = ["project_id", "store_name", "region", "address", "urgency", "start_time", "end_time", "payment_base_price", "agency_price", "supervisor_id", "operator_id", "store_remarks", "task_sop_html"]
    rows = [["1", "示例门店", "华东一区", "上海市示例路100号", "一般", date.today().strftime("%Y-%m-%d"), date.today().strftime("%Y-%m-%d"), "50", "80", "", "", "备注", "<p>到店执行 SOP</p>"]]
    return export_csv("task_import_template.csv", headers, rows)


@app.route("/tasks/import", methods=["POST"])
@login_required
@role_required("super_admin", "supervisor")
def import_tasks():
    file = request.files.get("csv_file")
    if not file or not file.filename:
        flash("❌ 请上传 CSV 文件", "danger")
        return redirect(url_for("tasks"))
    raw = file.read()
    text = None
    for encoding in ("utf-8-sig", "utf-8", "gbk", "gb18030", "latin-1"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        flash("❌ 文件编码无法识别，请使用 UTF-8 CSV 模板导入", "danger")
        return redirect(url_for("tasks"))
    reader = csv.DictReader(io.StringIO(text))
    required_headers = {"project_id", "store_name", "start_time", "end_time"}
    if not reader.fieldnames or not required_headers.issubset(set(reader.fieldnames)):
        flash("❌ 导入文件缺少必要字段：project_id、store_name、start_time、end_time", "danger")
        return redirect(url_for("tasks"))
    count = 0
    failed = []
    skipped = 0
    for line_no, row in enumerate(reader, start=2):
        if not any((v or "").strip() for v in row.values()):
            skipped += 1
            continue
        store_name = (row.get("store_name") or "").strip()
        if not store_name:
            failed.append(f"第 {line_no} 行：门店名称为空")
            continue
        if len(store_name) > 120 or has_invalid_text(store_name):
            failed.append(f"第 {line_no} 行：门店名称包含异常字符或长度超过 120")
            continue
        for field_name, label in [("region", "区域"), ("address", "地址"), ("store_remarks", "备注")]:
            value = row.get(field_name) or ""
            if len(value) > 500 or has_invalid_text(value):
                failed.append(f"第 {line_no} 行：{label}包含异常字符或过长")
                break
        else:
            pass
        if failed and failed[-1].startswith(f"第 {line_no} 行：") and ("异常字符" in failed[-1] or "过长" in failed[-1]):
            continue
        if not (row.get("start_time") or "").strip() or not (row.get("end_time") or "").strip():
            failed.append(f"第 {line_no} 行：开始时间和截止时间不能为空")
            continue
        start_time, end_time, date_error = dates_are_valid(row.get("start_time"), row.get("end_time"))
        if date_error:
            failed.append(f"第 {line_no} 行：{date_error}")
            continue
        payment_base_price, price_error = parse_required_positive_float(row.get("payment_base_price"), "打款基准价")
        if price_error:
            failed.append(f"第 {line_no} 行：{price_error}")
            continue
        agency_price = None
        if current_user.role == "super_admin" and "agency_price" in (reader.fieldnames or []):
            raw_agency = (row.get("agency_price") or "").strip()
            if raw_agency:
                agency_price = parse_float(raw_agency, None)
                if agency_price is None or agency_price < 0:
                    failed.append(f"第 {line_no} 行：代理价格格式错误或不能为负数")
                    continue
        try:
            project_id = int(row.get("project_id") or 1)
            if not Project.query.get(project_id):
                raise ValueError("项目不存在")
        except Exception:
            failed.append(f"第 {line_no} 行：project_id 无效或项目不存在")
            continue
        supervisor_id = row.get("supervisor_id") or (current_employee_id() if current_user.role == "supervisor" else None)
        operator_id = row.get("operator_id") or None
        try:
            task = Task(
                code=f"XF{utc_now().strftime('%Y%m%d%H%M%S')}{secrets.token_hex(2).upper()}",
                project_id=project_id,
                creator_id=current_user.id,
                supervisor_id=int(supervisor_id) if supervisor_id else None,
                operator_id=int(operator_id) if operator_id else None,
                store_name=store_name,
                region=(row.get("region") or "未分区").strip() or "未分区",
                address=(row.get("address") or "").strip(),
                urgency=(row.get("urgency") or "一般").strip() or "一般",
                start_time=start_time,
                end_time=end_time,
                payment_base_price=payment_base_price,
                agency_price=agency_price,
                store_remarks=row.get("store_remarks", ""),
                task_sop_html=row.get("task_sop_html", ""),
                task_status="待运营承接" if operator_id else "待主管承接",
                confirmation_token=secrets.token_urlsafe(32),
            )
            db.session.add(task)
            db.session.flush()
            add_flow(task, "批量导入门店", after=f"门店：{task.store_name}")
            count += 1
        except Exception as exc:
            failed.append(f"第 {line_no} 行：{exc}")
    log_operation("批量导入", "导入门店任务", f"成功 {count} 条，失败 {len(failed)} 条，跳过空行 {skipped} 条")
    db.session.commit()
    msg = f"✅ 导入完成：成功 {count} 条，失败 {len(failed)} 条，跳过空行 {skipped} 条。"
    if failed:
        msg += " 失败详情：" + "；".join(failed[:8]) + ("；……" if len(failed) > 8 else "")
        flash(msg, "warning")
    else:
        flash(msg, "success")
    return redirect(url_for("tasks"))


@app.route("/tasks/batch", methods=["POST"])
@login_required
@role_required("super_admin", "supervisor")
def batch_update_tasks():
    ids = request.form.getlist("task_ids")
    if not ids:
        flash("❌ 请勾选至少一个任务", "danger")
        return redirect(url_for("tasks"))
    urgency = request.form.get("batch_urgency")
    start_time = request.form.get("batch_start_time")
    end_time = request.form.get("batch_end_time")
    operator_id = request.form.get("batch_operator_id")
    if start_time and end_time:
        _, _, date_error = dates_are_valid(start_time, end_time)
        if date_error:
            flash(f"❌ {date_error}", "danger")
            return redirect(url_for("tasks"))
    updated = 0
    for task in Task.query.filter(Task.id.in_([int(x) for x in ids])).all():
        if not can_access_task(task):
            continue
        before = f"紧急度：{task.urgency}；时间：{task.start_time}~{task.end_time}；运营：{task.operator.name if task.operator else '无'}"
        if urgency:
            task.urgency = urgency
        if start_time:
            task.start_time = parse_date(start_time, task.start_time)
        if end_time:
            task.end_time = parse_date(end_time, task.end_time)
        if operator_id:
            task.operator_id = int(operator_id)
            task.task_status = "待运营承接"
        after = f"紧急度：{task.urgency}；时间：{task.start_time}~{task.end_time}；运营：{task.operator.name if task.operator else '无'}"
        add_flow(task, "批量设置门店信息", before, after)
        updated += 1
    db.session.commit()
    flash(f"✅ 已批量更新 {updated} 条任务", "success")
    return redirect(url_for("tasks"))


@app.route("/export/tasks")
@login_required
def export_tasks():
    rows = []
    for t in apply_task_filters(visible_tasks_query()).order_by(Task.created_at.desc()).all():
        row = [
            t.code, t.project.project_name, t.store_name, t.region, t.address, t.urgency,
            t.start_time, t.end_time, t.countdown, t.task_status, t.audit_status,
            t.supervisor.name if t.supervisor else "", t.operator.name if t.operator else "",
            t.payment_base_price, t.approved_extra_price, t.final_payment_price,
        ]
        if current_user.role == "super_admin":
            row.append(t.agency_price or "")
        row.extend([t.executor_name, t.executor_phone, t.payee_name, t.payee_phone, t.payee_bank, t.payee_account, t.confirmation_status])
        rows.append(row)
    headers = ["工单号", "项目", "门店", "区域", "地址", "紧急度", "开始", "截止", "倒计时", "任务状态", "审核状态", "主管", "运营", "基准价", "已通过加价", "打款价"]
    if current_user.role == "super_admin":
        headers.append("代理价")
    headers.extend(["执行人", "执行人手机号", "收款人", "收款人手机号", "开户行", "收款账号", "第三方确认状态"])
    return export_csv("tasks_export.csv", headers, rows)


@app.route("/export/flows")
@login_required
def export_flows():
    task_ids = [t.id for t in visible_tasks_query().all()] or [0]
    flows = StoreFlowRecord.query.filter(StoreFlowRecord.task_id.in_(task_ids)).order_by(StoreFlowRecord.created_at.desc()).all()
    rows = [[f.created_at, f.task.code, f.task.store_name, f.operator_name, f.action, f.before_text, f.after_text] for f in flows]
    return export_csv("flow_records.csv", ["时间", "工单号", "门店", "操作人", "动作", "变更前", "变更后"], rows)


@app.route("/export/travel")
@login_required
def export_travel():
    task_ids = [t.id for t in visible_tasks_query().all()] or [0]
    items = TravelSubsidy.query.filter(TravelSubsidy.task_id.in_(task_ids)).order_by(TravelSubsidy.created_at.desc()).all()
    rows = [[s.created_at, s.task.code, s.task.store_name, s.applicant.display_name, s.amount, s.reason, s.status, s.supervisor_comment, s.admin_comment] for s in items]
    return export_csv("travel_subsidies.csv", ["时间", "工单号", "门店", "申请人", "金额", "原因", "状态", "主管意见", "超管意见"], rows)


@app.route("/export/confirmations")
@login_required
def export_confirmations():
    rows = []
    for t in visible_tasks_query().order_by(Task.created_at.desc()).all():
        rows.append([
            t.code, t.store_name, t.confirmation_status, t.confirmation_note, t.confirmation_submitted_at,
            t.confirmation_sent_to, t.confirmation_sent_at, t.confirmation_review_status,
            t.confirmation_review_note, t.confirmation_reviewed_at,
            t.confirmation_screenshot, url_for("confirm_execution", token=t.confirmation_token, _external=True)
        ])
    return export_csv(
        "confirmations.csv",
        ["工单号", "门店", "确认状态", "说明", "提交时间", "发送对象", "发送时间", "截图核对状态", "核对意见", "核对时间", "截图路径", "确认链接"],
        rows
    )


@app.route("/reports")
@login_required
def reports():
    month = request.args.get("month", date.today().strftime("%Y-%m"))
    completion_rows = calculate_monthly_completion(month)
    tasks_list = visible_tasks_query().all()
    total_payment = round(sum(t.final_payment_price for t in tasks_list), 2)
    total_agency = round(sum((t.agency_price or 0) for t in tasks_list), 2) if current_user.role == "super_admin" else None
    return render_template("reports.html", month=month, completion_rows=completion_rows, total_payment=total_payment, total_agency=total_agency)


@app.route("/export/monthly")
@login_required
def export_monthly():
    month = request.args.get("month", date.today().strftime("%Y-%m"))
    rows = []
    for r in calculate_monthly_completion(month):
        rows.append([month, r["employee"].name, r["assigned"], r["target"], r["completed"], f"{r['rate']}%"])
    return export_csv("monthly_completion.csv", ["月份", "运营", "本月分配任务", "月度应完成", "月度实际完成", "完成率"], rows)


# ============================================================
# 上传文件访问
# ============================================================
@app.route("/uploads/<path:filename>")
@login_required
def uploaded_file(filename):
    return send_from_directory(app.config["UPLOAD_FOLDER"], filename)


# ============================================================
# 启动入口
# ============================================================
if __name__ == "__main__":
    with app.app_context():
        db.create_all()
        seed_data()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
