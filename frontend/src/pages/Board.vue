<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { api } from '../api.js'

const waybills = ref([])
const selected = ref(null)
const detail = ref(null)
const overview = ref(null)
const err = ref('')

async function loadList() {
  overview.value = await api.overview()
  waybills.value = await api.waybills()
  if (!selected.value && waybills.value.length) {
    selected.value = waybills.value[0].id
  }
}

async function loadDetail(id) {
  if (!id) return
  detail.value = await api.timeline(id)
}

onMounted(async () => {
  try {
    await loadList()
  } catch (e) {
    err.value = String(e.message || e)
  }
})

watch(selected, (id) => {
  loadDetail(id).catch((e) => { err.value = String(e.message || e) })
})

const hops = computed(() => detail.value?.hops || [])
const quarantined = computed(() => detail.value?.quarantined_hops || [])
const proof = computed(() => detail.value?.proof)
const skipProof = computed(() => detail.value?.route_skip_proof || null)
const tear = computed(() => detail.value?.skip_tear_verification || null)
const tearCheck = computed(() => detail.value?.tear_check || null)
const declaredRoute = computed(() => detail.value?.declared_route || [])

const vTypeText = (t) => (t === 'skip' ? '跳过未声明下一站' : t === 'undeclared' ? '插入未声明站' : t)
</script>

<template>
  <!-- plant-ui -->

  <div>
    <p v-if="err" class="muted">{{ err }}</p>
    <div class="stat-row" v-if="overview">
      <div class="stat"><b>{{ overview.waybills }}</b><span>运单</span></div>
      <div class="stat"><b>{{ overview.hops_primary }}</b><span>正式 hop</span></div>
      <div class="stat"><b>{{ overview.hops_sidecar }}</b><span>旁路</span></div>
      <div class="stat"><b>{{ overview.quarantine }}</b><span>隔离</span></div>
      <div class="stat danger-stat"><b>{{ overview.route_skip_proofs ?? 0 }}</b><span>跳站证明</span></div>
      <div class="stat danger-stat"><b>{{ overview.skip_tear_verifications ?? 0 }}</b><span>撕裂核证</span></div>
    </div>

    <div class="tri">
      <aside class="pane waybill-pane">
        <h2>运单索引</h2>
        <button
          v-for="w in waybills"
          :key="w.id"
          class="wb-item"
          :class="{ active: selected === w.id }"
          @click="selected = w.id"
        >
          <span class="id">{{ w.id }}</span>
          <span class="title">{{ w.title }}</span>
          <span class="st">{{ w.status_proj }}</span>
          <span v-if="!w.axis_legal_hop_id && w.status_proj === '未知'" class="flag flag-danger">全段隔离</span>
          <span v-else-if="w.status_proj" class="flag flag-ok">轴 {{ w.axis_legal_hop_id || '—' }}</span>
        </button>
      </aside>

      <section class="pane axis-pane">
        <div class="axis-head">
          <h1>{{ detail?.waybill?.title || '时间线' }}</h1>
          <span>{{ selected }}</span>
        </div>

        <div class="route-strip" v-if="declaredRoute.length">
          <span class="route-label">声明路径</span>
          <template v-for="(code, i) in declaredRoute" :key="code + i">
            <span class="route-code">{{ code }}</span>
            <span v-if="i < declaredRoute.length - 1" class="route-arrow">→</span>
          </template>
        </div>

        <h2 class="axis-sub">正式有效轴（合法前缀）</h2>
        <div class="v-axis" v-if="hops.length">
          <div class="hop-node" v-for="h in hops" :key="h.id">
            <div class="ts">{{ h.event_ts }}</div>
            <div class="code">{{ h.event_code }}</div>
            <div class="loc">{{ h.location_code }} · {{ h.id }}</div>
          </div>
          <div class="axis-stop" v-if="quarantined.length">
            轴停于 {{ hops[hops.length - 1].id }} · 后续 {{ quarantined.length }} 点整段隔离
            <span v-if="tear" class="axis-stop-meta">
              （投影 {{ tear.projection_before }} → {{ tear.projection_after }}，
              非法跳站点 {{ tear.projection_before_hop_id }} 未推进投影）
            </span>
          </div>
        </div>
        <p v-else class="muted">无合法正式轨迹点（首点即违规，正式轴为空）</p>

        <template v-if="quarantined.length">
          <h2 class="axis-sub danger-text">隔离段（不进正式轴 · 不派生投影）</h2>
          <div class="v-axis v-axis-quar">
            <div class="hop-node hop-node-quar" v-for="h in quarantined" :key="h.id">
              <div class="ts">{{ h.event_ts }}</div>
              <div class="code">{{ h.event_code }}</div>
              <div class="loc">{{ h.location_code }} · {{ h.id }}</div>
            </div>
          </div>
        </template>
      </section>

      <aside class="pane inspect-pane">
        <h2>检查器</h2>
        <dl class="kv" v-if="detail?.waybill">
          <dt>运单</dt><dd>{{ detail.waybill.id }}</dd>
          <dt>投影状态</dt><dd>{{ detail.waybill.status_proj }}</dd>
          <dt>轴停点</dt><dd>{{ detail.waybill.axis_legal_hop_id || '空（提交）' }}</dd>
          <dt>创建</dt><dd>{{ detail.waybill.created_at }}</dd>
        </dl>

        <template v-if="skipProof">
          <h2>跳站证明</h2>
          <dl class="kv">
            <dt>违规点</dt><dd>{{ skipProof.violation_hop_id }}</dd>
            <dt>违规时间</dt><dd>{{ skipProof.violation_ts }}</dd>
            <dt>类型</dt><dd>{{ vTypeText(skipProof.violation_type) }}</dd>
            <dt>预期下一站</dt><dd class="danger-text">{{ skipProof.expected_code }}</dd>
            <dt>实际站</dt><dd class="danger-text">{{ skipProof.actual_code }}</dd>
            <dt>发现批次</dt><dd>{{ skipProof.detected_batch_id }}</dd>
            <dt>隔离成员</dt><dd>{{ skipProof.members.map((m) => m.hop_id).join(', ') }}</dd>
          </dl>
        </template>

        <template v-if="tear">
          <h2>跳站撕裂核证</h2>
          <dl class="kv">
            <dt>互证</dt>
            <dd :class="tearCheck?.consistent ? 'ok-text' : 'danger-text'">
              {{ tearCheck?.consistent ? '轴停点 / 投影 / 隔离原因一致' : '存在分叉，见证明页' }}
            </dd>
            <dt>声明路径</dt><dd class="route-cell">{{ tear.declared_path.join(' → ') }}</dd>
            <dt>违规点</dt><dd>{{ tear.violation_hop_id }}</dd>
            <dt>合法前缀末 hop</dt><dd>{{ tear.legal_prefix_last_hop_id || '无（全段隔离）' }}</dd>
            <dt>投影前</dt><dd>{{ tear.projection_before }} · {{ tear.projection_before_hop_id || '—' }}</dd>
            <dt>投影后</dt><dd class="ok-text">{{ tear.projection_after }} · {{ tear.projection_after_hop_id || '—' }}</dd>
            <dt>隔离成员</dt><dd>{{ tear.members.map((m) => m.hop_id).join(', ') }}</dd>
          </dl>
        </template>

        <h2>状态证明</h2>
        <dl class="kv" v-if="proof">
          <dt>末合法 hop</dt><dd>{{ proof.last_hop_id || '无' }}</dd>
          <dt>映射码</dt><dd>{{ proof.mapped_code || '—' }}</dd>
          <dt>投影值</dt><dd>{{ proof.proj_value }}</dd>
          <dt>写入</dt><dd>{{ proof.created_at }}</dd>
        </dl>
        <p v-else class="muted">暂无证明</p>
      </aside>

      <footer class="pane dot-rail">
        <span class="label">HOP</span>
        <template v-for="(h, i) in hops" :key="h.id">
          <span class="dot on"></span>
          <span v-if="i < hops.length - 1" class="dot-line"></span>
        </template>
        <template v-if="quarantined.length">
          <span class="dot-line cut"></span>
          <span v-for="h in quarantined" :key="'q' + h.id" class="dot dot-quar"></span>
        </template>
        <template v-if="!hops.length && !quarantined.length">
          <span class="dot"></span>
          <span class="dot-line"></span>
          <span class="dot"></span>
          <span class="dot-line"></span>
          <span class="dot"></span>
        </template>
      </footer>
    </div>
  </div>
</template>
