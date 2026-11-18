<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api.js'

const rows = ref([])
const skipRows = ref([])
const waybillFilter = ref('')
const batchFilter = ref('')
const err = ref('')

onMounted(async () => {
  rows.value = await api.proofs()
  await loadSkipProofs()
})

async function loadSkipProofs() {
  err.value = ''
  try {
    skipRows.value = await api.routeSkipProofs({
      waybillId: waybillFilter.value.trim() || undefined,
      batchId: batchFilter.value.trim() || undefined,
    })
  } catch (e) {
    err.value = String(e.message || e)
  }
}

function resetFilters() {
  waybillFilter.value = ''
  batchFilter.value = ''
  loadSkipProofs()
}

const typeText = (t) => (t === 'skip' ? '跳过未声明下一站' : t === 'undeclared' ? '插入未声明站' : t)
</script>

<template>
  <!-- plant-ui -->

  <div class="page-card">
    <h1>证明</h1>
    <p class="lead">状态投影证明与时间线末合法 hop 汇总；跳站证明含声明路径、违规点、预期/实际站与隔离成员快照。</p>

    <h2 class="section-title">跳站证明（只读）</h2>
    <div class="filter-bar">
      <label>运单
        <input v-model="waybillFilter" placeholder="如 WB-2001" @keyup.enter="loadSkipProofs" />
      </label>
      <label>批次
        <input v-model="batchFilter" placeholder="如 B-DEMO-01" @keyup.enter="loadSkipProofs" />
      </label>
      <button class="btn" @click="loadSkipProofs">查询</button>
      <button class="btn btn-ghost" @click="resetFilters">重置</button>
    </div>
    <p v-if="err" class="muted">{{ err }}</p>
    <table class="table">
      <thead>
        <tr>
          <th>id</th><th>运单</th><th>声明路径</th><th>违规点</th><th>预期站</th>
          <th>实际站</th><th>类型</th><th>隔离成员</th><th>发现批次</th><th>刷新时间</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="r in skipRows" :key="r.id">
          <td>{{ r.id }}</td>
          <td>{{ r.waybill_id }}</td>
          <td class="route-cell">{{ r.declared_path.join(' → ') }}</td>
          <td>{{ r.violation_hop_id }}<br /><span class="muted">{{ r.violation_ts }}</span></td>
          <td class="danger-text">{{ r.expected_code }}</td>
          <td class="danger-text">{{ r.actual_code }}</td>
          <td>{{ typeText(r.violation_type) }}</td>
          <td class="member-cell">
            <div v-for="m in r.members" :key="m.hop_id" class="member-chip">
              <b>{{ m.hop_id }}</b>
              <span>{{ m.location_code }} · {{ m.event_code }}</span>
            </div>
          </td>
          <td>{{ r.detected_batch_id }}</td>
          <td>{{ r.refreshed_at }}</td>
        </tr>
        <tr v-if="!skipRows.length"><td colspan="10" class="muted">暂无跳站证明</td></tr>
      </tbody>
    </table>

    <h2 class="section-title" style="margin-top:28px">状态投影证明</h2>
    <table class="table">
      <thead>
        <tr><th>id</th><th>运单</th><th>末合法 hop</th><th>映射码</th><th>投影</th><th>时间</th></tr>
      </thead>
      <tbody>
        <tr v-for="r in rows" :key="r.id">
          <td>{{ r.id }}</td>
          <td>{{ r.waybill_id }}</td>
          <td>{{ r.last_hop_id || '无（全段隔离）' }}</td>
          <td>{{ r.mapped_code || '—' }}</td>
          <td>{{ r.proj_value }}</td>
          <td>{{ r.created_at }}</td>
        </tr>
        <tr v-if="!rows.length"><td colspan="6" class="muted">暂无证明</td></tr>
      </tbody>
    </table>
  </div>
</template>
