<template>
  <section class="page" data-module="backfill_job">
    <header class="page-head">
      <div><h2>补数调度</h2><p class="page-desc">补数任务日期来自表级比对差异分区清单，支持回查差异键与对应左右表分区。</p></div>
      <div class="page-actions">
        <button class="btn primary" type="button" data-testid="open-create" @click="openCreate">登记补数任务</button>
        <button class="btn" type="button" data-testid="export" @click="exportRows">导出作业清单</button>
      </div>
    </header>
    <div class="filter-bar">
      <label><span>开始日期</span><input type="date" v-model="dateFrom" /></label>
      <label><span>结束日期</span><input type="date" v-model="dateTo" /></label>
      <button class="btn" type="button" @click="reload">查询</button>
    </div>
    <div class="timeline-wrap">
      <div v-for="item in timelineItems" :key="item.id" class="timeline-row">
        <span class="timeline-label">{{ item.name || item.label }}</span>
        <div class="timeline-track">
          <div class="timeline-bar" :class="`status-${item.status}`" :style="{ left: barLeft(item) + '%', width: barWidth(item) + '%' }" :data-testid="`bar-${item.id}`" :title="`${item.start} ~ ${item.end}`" @click="selectItem(item)">&nbsp;</div>
        </div>
        <span style="font-size:12px;color:var(--muted);margin-left:8px;">{{ item.start }} ~ {{ item.end }}</span>
        <button class="link" style="margin-left:10px;" type="button" :data-testid="`detail-${item.id}`" @click="openDetail(item)">详情</button>
      </div>
      <p v-if="!timelineItems.length" style="text-align:center;color:var(--muted)">暂无作业记录</p>
    </div>
    <div v-if="selectedItem" class="timeline-log" data-testid="timeline-log">
      <strong>{{ selectedItem.name || selectedItem.label }}</strong> — 状态：{{ selectedItem.status }}
      <div style="margin-top:4px;">{{ selectedItem.logs || '暂无日志' }}</div>
    </div>

    <div v-if="detail" class="modal-backdrop" data-testid="detail-modal">
      <div class="modal" style="width:min(1000px,100%);">
        <div style="display:flex;justify-content:space-between;align-items:center;">
          <h3 style="margin:0;">补数任务 · {{ detail.name }}</h3>
          <button class="btn ghost" type="button" data-testid="close-detail" @click="detail=null">关闭</button>
        </div>
        <p v-if="detailError" class="error-text">{{ detailError }}</p>
        <table class="data-table" style="margin-top:12px;" v-if="jobEntry">
          <tbody>
            <tr><th>目标表名</th><td>{{ jobEntry['目标表名'] }}</td><th>起始日期</th><td>{{ jobEntry['起始日期'] }}</td></tr>
            <tr><th>截止日期</th><td>{{ jobEntry['截止日期'] }}</td><th>覆盖策略</th><td>{{ jobEntry['覆盖策略'] }}</td></tr>
            <tr><th>完成水位</th><td>{{ jobEntry['完成水位'] || '—' }}</td><th>状态</th><td>{{ jobEntry.status }}</td></tr>
            <tr><th>失败区间</th><td>{{ jobEntry['失败区间'] || '—' }}</td><th>来源比对</th><td>{{ linkage?.source_compare_no || '手工登记' }}</td></tr>
          </tbody>
        </table>
        <p v-if="jobEntry?.error_message" class="error-text" data-testid="job-error">{{ jobEntry.error_message }}</p>
        <div style="display:flex;gap:8px;margin:12px 0;">
          <button class="btn primary" type="button" data-testid="run-job" @click="runJob">启动任务</button>
          <button class="btn" type="button" data-testid="finish-job" @click="finishJob">确认完成</button>
        </div>
        <h4 style="margin:8px 0 6px;">差异键与左右表分区回查</h4>
        <table class="data-table" data-testid="linkage-table">
          <thead><tr><th>序号</th><th>差异键</th><th>差异日期</th><th>左表日期</th><th>右表日期</th><th>补数范围</th><th>清单状态</th></tr></thead>
          <tbody>
            <tr v-for="row in linkage?.diff_rows || []" :key="row.seq">
              <td>{{ row.seq }}</td><td>{{ row.diff_key }}</td><td>{{ row.diff_date || '—' }}</td>
              <td>{{ row.left_date || '—' }}</td><td>{{ row.right_date || '—' }}</td>
              <td>{{ formatRange(row.backfill_range) }}</td><td>{{ row.status }}</td>
            </tr>
            <tr v-if="!(linkage?.diff_rows || []).length"><td colspan="7" class="empty-state">该任务未关联差异分区清单（可能为手工登记）</td></tr>
          </tbody>
        </table>
        <h4 style="margin:12px 0 6px;">实际补数分区</h4>
        <p style="font-size:13px;">
          <span v-for="p in linkage?.partitions || []" :key="p.dt" class="part-chip" data-testid="backfill-partition">{{ p.dt }}</span>
          <span v-if="!(linkage?.partitions || []).length" style="color:var(--muted)">尚未执行补数</span>
        </p>
      </div>
    </div>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate"><h3>登记补数任务</h3>
        <label v-for="field in columns.slice(0,3)" :key="field" class="create-field"><span>{{ field }}</span><input v-model="createValues[field]" :placeholder="`请输入${field}`" required /></label>
        <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:12px">
          <button class="btn" type="button" @click="showCreate=false">取消</button>
          <button class="btn primary" type="submit" data-testid="submit-create">提交</button>
        </div>
      </form>
    </div>
    <footer class="page-foot"><span v-if="errorMessage" class="error-text">{{ errorMessage }}</span></footer>
  </section>
</template>
<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { request } from '@/api/client'
const ENDPOINT = '/api/backfill_job'
const columns = ["任务编号", "目标表名", "起始日期", "截止日期", "并行度", "覆盖策略", "完成水位", "失败区间"]
const timelineItems = ref<any[]>([])
const selectedItem = ref<any>(null)
const dateFrom = ref(''); const dateTo = ref('')
const errorMessage = ref(''); const showCreate = ref(false)
const createValues = ref<Record<string,string>>({})
const allItems = ref<any[]>([])
const detail = ref<any>(null)
const jobEntry = ref<any>(null)
const linkage = ref<any>(null)
const detailError = ref('')
function barLeft(item: any) { const all = allItems.value.filter(i => i.start && i.end); if(!all.length) return 0; const min = Math.min(...all.map(i => new Date(i.start).getTime())); const max = Math.max(...all.map(i => new Date(i.end).getTime())); const range = max - min || 86400000; return ((new Date(item.start).getTime() - min) / range) * 100 }
function barWidth(item: any) { const all = allItems.value.filter(i => i.start && i.end); if(!all.length) return 20; const min = Math.min(...all.map(i => new Date(i.start).getTime())); const max = Math.max(...all.map(i => new Date(i.end).getTime())); const range = max - min || 86400000; return Math.max(5, ((new Date(item.end).getTime() - new Date(item.start).getTime()) / range) * 100) }
function selectItem(item: any) { selectedItem.value = selectedItem.value?.id === item.id ? null : item }
function formatRange(range: any) { if (!range || !range.start) return '—'; return `${range.start} ~ ${range.end}` }
async function reload() {
  try { const res = await request(ENDPOINT); const data = await res.json(); allItems.value = data.items || []; applyFilter() } catch(e) { errorMessage.value = '加载失败' }
}
function applyFilter() {
  let items = allItems.value
  if(dateFrom.value) items = items.filter((i: any) => i.start >= dateFrom.value)
  if(dateTo.value) items = items.filter((i: any) => i.end <= dateTo.value)
  timelineItems.value = items; selectedItem.value = null
}
async function openDetail(item: any) {
  detail.value = item; detailError.value = ''; jobEntry.value = null; linkage.value = null
  try {
    const [entryRes, linkageRes] = await Promise.all([
      request(`${ENDPOINT}/${item.id}`),
      request(`${ENDPOINT}/${item.id}/linkage`),
    ])
    if (!entryRes.ok || !linkageRes.ok) throw new Error('补数任务读取失败')
    jobEntry.value = await entryRes.json()
    linkage.value = await linkageRes.json()
  } catch (e: any) { detailError.value = e.message }
}
async function triggerAction(action: string) {
  if (!detail.value) return
  detailError.value = ''
  try {
    const res = await request(`${ENDPOINT}/${detail.value.id}/actions`, {
      method: 'POST', body: JSON.stringify({ action }),
    })
    const payload = await res.json()
    if (!res.ok || payload.ok === false) throw new Error(payload.message || '任务动作失败')
    await openDetail(detail.value)
    await reload()
  } catch (e: any) {
    detailError.value = e.message
    await openDetail(detail.value)
  }
}
function runJob() { return triggerAction('启动任务') }
function finishJob() { return triggerAction('确认完成') }
function openCreate() { createValues.value = {}; showCreate.value = true }
async function submitCreate() {
  try { const res = await request(ENDPOINT, { method: 'POST', body: JSON.stringify({ values: createValues.value }) }); if(!res.ok) throw new Error('创建失败'); showCreate.value = false; await reload() } catch(e: any) { errorMessage.value = e.message }
}
function exportRows() { window.open(`${ENDPOINT}/export`, '_blank') }
onMounted(reload)
</script>
<style scoped>
.part-chip { display:inline-block; background:#eef4ff; border:1px solid var(--border); border-radius:999px; padding:2px 10px; margin-right:6px; font-size:12px; }
</style>
