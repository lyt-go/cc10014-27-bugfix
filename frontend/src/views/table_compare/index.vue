<template>
  <section class="page" data-module="table_compare">
    <header class="page-head">
      <div><h2>表级比对</h2><p class="page-desc">按差异分区生成左右表差异记录，差异分区清单串联补数调度，补数日期严格来自差异清单。</p></div>
      <div class="page-actions">
        <button class="btn primary" type="button" data-testid="open-create" @click="openCreate">登记比对任务</button>
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
        <span style="font-size:12px;color:var(--muted);margin-left:8px;">差异 {{ item.diff_count }} 行 / {{ item.partition_count }} 分区</span>
        <button class="link" style="margin-left:10px;" type="button" :data-testid="`detail-${item.id}`" @click="openDetail(item)">详情</button>
      </div>
      <p v-if="!timelineItems.length" style="text-align:center;color:var(--muted)">暂无作业记录</p>
    </div>
    <div v-if="selectedItem" class="timeline-log" data-testid="timeline-log">
      <strong>{{ selectedItem.name || selectedItem.label }}</strong> — 状态：{{ selectedItem.status }}
      <div style="margin-top:4px;">{{ selectedItem.logs || '暂无日志' }}</div>
    </div>

    <div v-if="detail" class="modal-backdrop" data-testid="detail-modal">
      <div class="modal" style="width:min(1080px,100%);">
        <div style="display:flex;justify-content:space-between;align-items:center;">
          <h3 style="margin:0;">比对结果 · {{ detail.name }}</h3>
          <button class="btn ghost" type="button" data-testid="close-detail" @click="detail=null">关闭</button>
        </div>
        <p v-if="detailError" class="error-text" data-testid="detail-error">{{ detailError }}</p>
        <div v-if="compareResult" class="stat-row" style="margin-top:12px;">
          <article class="stat-card"><span class="stat-label">左表行数</span><strong class="stat-value">{{ compareResult.left_count }}</strong></article>
          <article class="stat-card"><span class="stat-label">右表行数</span><strong class="stat-value">{{ compareResult.right_count }}</strong></article>
          <article class="stat-card"><span class="stat-label">差异总数</span><strong class="stat-value">{{ compareResult.diff_count }}</strong></article>
          <article class="stat-card"><span class="stat-label">差异分区数</span><strong class="stat-value">{{ compareResult.partition_count }}</strong></article>
        </div>
        <div style="display:flex;gap:8px;margin:10px 0;flex-wrap:wrap;">
          <button class="btn primary" type="button" data-testid="run-strict" @click="runCompare('strict')">重新比对（严格边界）</button>
          <button class="btn" type="button" data-testid="run-relaxed" @click="runCompare('relaxed')">重新比对（放宽边界）</button>
          <span style="font-size:12px;color:var(--muted);align-self:center;">当前边界模式：{{ compareResult?.boundary_mode || '—' }}</span>
        </div>
        <p v-if="compareResult?.error_message" class="error-text" data-testid="compare-error">{{ compareResult.error_message }}</p>
        <h4 style="margin:12px 0 6px;">差异分区清单（差异日期、差异键、左右日期与补数范围）</h4>
        <div style="max-height:46vh;overflow:auto;">
          <table class="data-table" data-testid="diff-table">
            <thead>
              <tr>
                <th>序号</th><th>差异类型</th><th>差异键</th><th>差异日期</th>
                <th>左表日期</th><th>右表日期</th><th>忽略侧</th>
                <th>左表补数范围</th><th>右表补数范围</th><th>补数任务</th><th>状态</th><th>冲突处理</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="row in diffRows" :key="`${row.compare_id}-${row.seq}`" :data-testid="`diff-row-${row.seq}`" :data-status="row.status">
                <td>{{ row.seq }}</td>
                <td>{{ row.diff_type }}</td>
                <td>{{ row.diff_key }}</td>
                <td>{{ row.diff_date || '—' }}</td>
                <td>{{ row.left_date || '—' }}</td>
                <td>{{ row.right_date || '—' }}</td>
                <td>{{ ignoredLabel(row.ignored_side) }}</td>
                <td>{{ formatRange(row.left_backfill_range) }}</td>
                <td>{{ formatRange(row.right_backfill_range) }}</td>
                <td>{{ row.backfill_job_no || '—' }}</td>
                <td>{{ row.status }}</td>
                <td class="row-actions">
                  <template v-if="row.status === '冲突阻塞'">
                    <button class="link" type="button" @click="resolve(row, 'take_over')">接管</button>
                    <button class="link" type="button" @click="resolve(row, 'accept_priority')">让位</button>
                  </template>
                  <span v-else-if="row.priority">{{ row.priority }}</span>
                  <span v-else>—</span>
                </td>
              </tr>
              <tr v-if="!diffRows.length"><td colspan="12" class="empty-state">暂无差异分区记录</td></tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate"><h3>登记比对任务</h3>
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
const ENDPOINT = '/api/table_compare'
const columns = ["任务编号", "左表名称", "右表名称", "比对字段", "比对方式", "差异阈值", "采样比例", "比对批次"]
const timelineItems = ref<any[]>([])
const selectedItem = ref<any>(null)
const dateFrom = ref(''); const dateTo = ref('')
const errorMessage = ref(''); const showCreate = ref(false)
const createValues = ref<Record<string,string>>({})
const allItems = ref<any[]>([])
const detail = ref<any>(null)
const compareResult = ref<any>(null)
const diffRows = ref<any[]>([])
const detailError = ref('')
function barLeft(item: any) { const all = allItems.value.filter(i => i.start && i.end); if(!all.length) return 0; const min = Math.min(...all.map(i => new Date(i.start).getTime())); const max = Math.max(...all.map(i => new Date(i.end).getTime())); const range = max - min || 86400000; return ((new Date(item.start).getTime() - min) / range) * 100 }
function barWidth(item: any) { const all = allItems.value.filter(i => i.start && i.end); if(!all.length) return 20; const min = Math.min(...all.map(i => new Date(i.start).getTime())); const max = Math.max(...all.map(i => new Date(i.end).getTime())); const range = max - min || 86400000; return Math.max(5, ((new Date(item.end).getTime() - new Date(item.start).getTime()) / range) * 100) }
function selectItem(item: any) { selectedItem.value = selectedItem.value?.id === item.id ? null : item }
function ignoredLabel(side: string) { return side === 'left' ? '忽略左表日期' : side === 'right' ? '忽略右表日期' : '—' }
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
  detail.value = item; detailError.value = ''; compareResult.value = null; diffRows.value = []
  try {
    const [resultRes, partitionRes] = await Promise.all([
      request(`${ENDPOINT}/${item.id}/result`),
      request(`${ENDPOINT}/${item.id}/diff-partitions`),
    ])
    if (!resultRes.ok || !partitionRes.ok) throw new Error('比对结果读取失败')
    compareResult.value = await resultRes.json()
    const partitionPayload = await partitionRes.json()
    diffRows.value = partitionPayload.items || []
  } catch (e: any) { detailError.value = e.message }
}
async function runCompare(mode: string) {
  if (!detail.value) return
  detailError.value = ''
  try {
    const res = await request(`${ENDPOINT}/${detail.value.id}/actions`, {
      method: 'POST', body: JSON.stringify({ action: '启动比对', mode }),
    })
    const payload = await res.json()
    if (!res.ok || payload.ok === false) throw new Error(payload.message || '比对执行失败')
    await openDetail(detail.value)
    await reload()
  } catch (e: any) {
    detailError.value = e.message
    await openDetail(detail.value)
  }
}
async function resolve(row: any, decision: string) {
  if (!detail.value) return
  try {
    const res = await request(`${ENDPOINT}/${detail.value.id}/diff-partitions/${row.seq}/resolve`, {
      method: 'POST', body: JSON.stringify({ action: decision }),
    })
    const payload = await res.json()
    if (!res.ok || payload.ok === false) throw new Error(payload.message || '冲突裁决失败')
    await openDetail(detail.value)
  } catch (e: any) { detailError.value = e.message }
}
function openCreate() { createValues.value = {}; showCreate.value = true }
async function submitCreate() {
  try { const res = await request(ENDPOINT, { method: 'POST', body: JSON.stringify({ values: createValues.value }) }); if(!res.ok) throw new Error('创建失败'); showCreate.value = false; await reload() } catch(e: any) { errorMessage.value = e.message }
}
function exportRows() { window.open(`${ENDPOINT}/export`, '_blank') }
onMounted(reload)
</script>
