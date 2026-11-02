import { createApp } from 'vue'
import { createRouter, createWebHashHistory } from 'vue-router'
import App from './App.vue'
import TemplatesView from './views/TemplatesView.vue'
import ProjectsView from './views/ProjectsView.vue'
import ProjectDetailView from './views/ProjectDetailView.vue'
import EditorView from './views/EditorView.vue'
import './styles.css'

const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: '/', redirect: '/templates' },
    { path: '/templates', name: 'templates', component: TemplatesView },
    { path: '/projects', name: 'projects', component: ProjectsView },
    { path: '/projects/:id', name: 'project-detail', component: ProjectDetailView, props: true },
    { path: '/projects/:id/edit', name: 'editor', component: EditorView, props: true }
  ]
})

createApp(App).use(router).mount('#app')
