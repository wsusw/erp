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
  updateSelectedCount();
}

function updateSelectedCount() {
  const checked = document.querySelectorAll('input[name="task_ids"]:checked');
  const badge = document.getElementById('selected-count');
  const num = document.getElementById('selected-num');
  if (!badge || !num) return;
  if (checked.length > 0) {
    num.textContent = checked.length;
    badge.style.display = '';
  } else {
    badge.style.display = 'none';
  }
}

// 事件委托：勾选 / 取消勾选时自动更新计数（兼容 HTMX 动态替换 DOM）
document.addEventListener('change', function(e) {
  if (e.target && e.target.name === 'task_ids') {
    updateSelectedCount();
  }
});

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
  const editor = document.getElementById('sopEditor');
  if (!editor) return;
  // 先聚焦编辑器再执行命令，确保选区在编辑区内，避免焦点/选区丢失导致删除键失灵
  editor.focus();
  document.execCommand(command, false, value);
}

function insertRichLink(type) {
  const url = prompt(type === 'video' ? '请输入视频 URL（mp4/mov 或可访问链接）' : '请输入图片 URL');
  if (!url) return;
  const editor = document.getElementById('sopEditor');
  if (!editor) return;
  editor.focus();
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
  const file = document.getElementById('confirmation_screenshot');
  if (file && !file.value) {
    alert('必须上传 APP 端报告提交成功截图。');
    file.focus();
    return false;
  }
  return true;
}

// 第三方确认页：截图始终必填。
(function() {
  const file = document.getElementById('confirmation_screenshot');
  if (file) file.required = true;
})();

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
// 10 秒超时自动恢复 + bfcache 恢复，避免提交失败后按钮永久卡死在"处理中…"。
document.addEventListener('submit', (event) => {
  const form = event.target;
  if (!(form instanceof HTMLFormElement)) return;
  setTimeout(() => {
    if (event.defaultPrevented) return;
    const buttons = form.querySelectorAll('button[type="submit"], button:not([type])');
    buttons.forEach(btn => {
      if (btn.disabled) return;
      btn.disabled = true;
      btn.dataset.originalText = btn.textContent;
      btn.dataset.submitBusy = 'true';
      btn.textContent = '处理中…';
    });
    // 10 秒后自动恢复：覆盖网络超时、服务端报错等未跳转场景
    const timer = setTimeout(() => {
      buttons.forEach(btn => {
        if (btn.dataset.submitBusy === 'true') {
          btn.disabled = false;
          btn.textContent = btn.dataset.originalText || btn.textContent;
          delete btn.dataset.submitBusy;
        }
      });
    }, 10000);
    // 页面正常跳转时清理定时器
    window.addEventListener('pagehide', () => clearTimeout(timer), { once: true });
  }, 0);
});

// 从浏览器 bfcache 恢复页面时，重置可能卡在"处理中…"的按钮
window.addEventListener('pageshow', (event) => {
  if (event.persisted) {
    document.querySelectorAll('button[data-submit-busy="true"]').forEach(btn => {
      btn.disabled = false;
      btn.textContent = btn.dataset.originalText || btn.textContent;
      delete btn.dataset.submitBusy;
    });
  }
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
