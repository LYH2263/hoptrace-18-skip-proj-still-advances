<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api.js'

const rows = ref([])
onMounted(async () => { rows.value = await api.quarantine() })

const typeText = (t) => (t === 'skip' ? '跳过未声明下一站' : t === 'undeclared' ? '插入未声明站' : t || '—')
</script>

<template>
  <!-- plant-ui -->

  <div class="page-card">
    <h1>隔离</h1>
    <p class="lead">跳站点起整段隔离：成员不进正式轴、不派生投影；预期/实际与隔离原因汇总刷新。</p>
    <table class="table">
      <thead>
        <tr>
          <th>id</th><th>运单</th><th>原因</th><th>预期下一站</th><th>实际站</th>
          <th>违规类型</th><th>声明路径</th><th>隔离成员</th><th>时间</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="r in rows" :key="r.id">
          <td>{{ r.id }}</td>
          <td>{{ r.waybill_id }}</td>
          <td>{{ r.reason }}</td>
          <td class="danger-text">{{ r.expected_location || '—' }}</td>
          <td class="danger-text">{{ r.actual_location || '—' }}</td>
          <td>{{ typeText(r.violation_type) }}</td>
          <td class="route-cell">{{ (r.declared_path || []).join(' → ') }}</td>
          <td class="member-cell">
            <div v-for="m in r.members" :key="m.hop_id" class="member-chip">
              <b>{{ m.hop_id }}</b>
              <span>{{ m.event_ts }} · {{ m.location_code }} · {{ m.event_code }}</span>
            </div>
          </td>
          <td>{{ r.created_at }}</td>
        </tr>
        <tr v-if="!rows.length"><td colspan="9" class="muted">暂无隔离项</td></tr>
      </tbody>
    </table>
  </div>
</template>
