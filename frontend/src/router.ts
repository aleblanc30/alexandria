import { createRouter, createWebHashHistory } from 'vue-router'

export const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: '/',           redirect: '/browse' },
    { path: '/search',     redirect: '/browse' },
    { path: '/browse',     component: () => import('@/views/BrowseView.vue') },
    { path: '/clusters',   component: () => import('@/views/ClusterView.vue') },
    { path: '/trends',     component: () => import('@/views/TrendsView.vue') },
    { path: '/tags',       component: () => import('@/views/TagView.vue') },
    { path: '/runs',       component: () => import('@/views/RunManagerView.vue') },
    { path: '/ingestion',         component: () => import('@/views/IngestionView.vue') },
    { path: '/ingestion/:source', component: () => import('@/views/IngestionSourceView.vue') },
    { path: '/lists',      component: () => import('@/views/ReadingListView.vue') },
  ],
})
