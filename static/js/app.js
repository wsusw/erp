function updateClock() {
  const el = document.getElementById('clock');
  if (!el) return;
  const d = new Date();
  const pad = n => String(n).padStart(2, '0');
  el.textContent = `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}
setInterval(updateClock, 1000);
updateClock();

setTimeout(() => {
  document.querySelectorAll('.toast').forEach(t => {
    t.style.opacity = '0';
    t.style.transform = 'translateY(-8px)';
    t.style.transition = '.35s ease';
    setTimeout(() => t.remove(), 400);
  });
}, 3200);

function toggleBox(id) {
  const el = document.getElementById(id);
  if (el) el.classList.toggle('hidden');
}

function checkAll(source) {
  document.querySelectorAll('input[name="task_ids"]').forEach(cb => cb.checked = source.checked);
}

function fillComment(select, targetId) {
  const target = document.getElementById(targetId);
  if (target && select.value) target.value = select.value;
}

function toggleConfirmWarning() {
  const s = document.getElementById('confirmation_status');
  const w = document.getElementById('confirmWarning');
  if (!s || !w) return;
  if (s.value === '已执行已提交') w.classList.remove('hidden');
  else w.classList.add('hidden');
}

function copyField(id) {
  const field = document.getElementById(id);
  if (!field) return;
  field.select();
  field.setSelectionRange(0, 99999);
  navigator.clipboard?.writeText(field.value).then(() => alert('链接已复制')).catch(() => {
    document.execCommand('copy');
    alert('链接已复制');
  });
}

function richCmd(command, value = null) {
  document.execCommand(command, false, value);
  const editor = document.getElementById('sopEditor');
  if (editor) editor.focus();
}

function insertRichLink(type) {
  const url = prompt(type === 'video' ? '请输入视频 URL（mp4/mov 或可访问链接）' : '请输入图片 URL');
  if (!url) return;
  if (type === 'video') {
    document.execCommand('insertHTML', false, `<p><video controls style="max-width:100%;border-radius:12px" src="${url}"></video></p>`);
  } else {
    document.execCommand('insertHTML', false, `<p><img style="max-width:100%;border-radius:12px" src="${url}" alt="SOP图片"></p>`);
  }
}

function syncRichEditor(editorId, textareaId) {
  const editor = document.getElementById(editorId);
  const textarea = document.getElementById(textareaId);
  if (editor && textarea) textarea.value = editor.innerHTML;
  return true;
}

function validateConfirmationForm() {
  const status = document.getElementById('confirmation_status');
  const file = document.getElementById('confirmation_screenshot');
  if (status && file && status.value === '已执行已提交' && !file.value) {
    alert('选择“已执行已提交”时，必须上传 APP 端报告提交成功截图。');
    file.focus();
    return false;
  }
  return true;
}

// 增强第三方确认页体验：选择“已执行已提交”时，文件控件变为必填。
const originalToggleConfirmWarning = window.toggleConfirmWarning;
window.toggleConfirmWarning = function() {
  if (typeof originalToggleConfirmWarning === 'function') originalToggleConfirmWarning();
  const s = document.getElementById('confirmation_status');
  const file = document.getElementById('confirmation_screenshot');
  if (!s || !file) return;
  file.required = s.value === '已执行已提交';
};

// 详情页查看/编辑模式切换
function toggleSectionEdit(sectionId) {
  const viewEl = document.getElementById(sectionId + '-view');
  const editEl = document.getElementById(sectionId + '-edit');
  const btnEl = document.getElementById('btn-edit-' + sectionId);
  if (!viewEl || !editEl) return;
  const isEditing = !editEl.classList.contains('hidden');
  if (isEditing) {
    // 切换回查看模式
    editEl.classList.add('hidden');
    viewEl.classList.remove('hidden');
    if (btnEl) { btnEl.textContent = '编辑'; btnEl.classList.remove('danger'); btnEl.classList.add('ghost'); }
  } else {
    // 切换到编辑模式
    viewEl.classList.add('hidden');
    editEl.classList.remove('hidden');
    if (btnEl) { btnEl.textContent = '取消编辑'; btnEl.classList.remove('ghost'); btnEl.classList.add('danger'); }
  }
}

// 防止重复提交。延迟禁用，避免浏览器原生必填校验和自定义校验被阻断。
document.addEventListener('submit', (event) => {
  const form = event.target;
  if (!(form instanceof HTMLFormElement)) return;
  setTimeout(() => {
    if (event.defaultPrevented) return;
    form.querySelectorAll('button[type="submit"], button:not([type])').forEach(btn => {
      btn.disabled = true;
      btn.dataset.originalText = btn.textContent;
      btn.textContent = '处理中…';
    });
  }, 0);
});

// 批量作废确认
function confirmBatchVoid() {
  var checked = document.querySelectorAll('input[name="task_ids"]:checked');
  if (checked.length === 0) {
    alert('请至少勾选一个任务');
    return false;
  }
  return confirm('确认批量作废已勾选的 ' + checked.length + ' 个门店任务？\n\n作废后任务池、统计、导出中将不再显示，但数据库和流转记录会保留。\n\n注意：已完结（已完成/放弃执行）的任务将被自动跳过。');
}
