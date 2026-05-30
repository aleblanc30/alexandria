<template>
  <div class="app">
    <AppSidebar />
    <main class="main">
      <RouterView />
    </main>
    <DocDetailPanel v-if="ui.detailOpen" />
    <ToastContainer />
  </div>
</template>

<script setup lang="ts">
import AppSidebar      from '@/components/AppSidebar.vue'
import DocDetailPanel  from '@/components/DocDetailPanel.vue'
import ToastContainer  from '@/components/ToastContainer.vue'
import { useUiStore }  from '@/stores/ui'
const ui = useUiStore()
</script>

<style>
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0 }
:root {
  --sidebar-w: 200px;
  --panel-w:   360px;
  --bg:        #f5f4f0;
  --surface:   #ffffff;
  --border:    rgba(0,0,0,.10);
  --text:      #1a1a18;
  --muted:     #6b6b68;
  --hint:      #a09f9a;
  --accent:    #378ADD;
  --radius:    8px;
  --radius-lg: 12px;
  font-family: system-ui, sans-serif;
  font-size: 14px;
  color: var(--text);
  background: var(--bg);
}
.app  { display: grid; grid-template-columns: var(--sidebar-w) 1fr; min-height: 100vh }
.main { padding: 24px; overflow: auto; min-width: 0 }
</style>
