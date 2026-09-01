import { createRouter, createWebHistory } from 'vue-router'
import GenerateView from '@/views/GenerateView.vue'
import AuditView from '@/views/AuditView.vue'
import SettingsView from '@/views/SettingsView.vue'

const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes: [
    { path: '/', redirect: '/generate' },
    { path: '/generate', name: 'generate', component: GenerateView },
    { path: '/audit', name: 'audit', component: AuditView },
    { path: '/settings', name: 'settings', component: SettingsView },
  ],
})

export default router
