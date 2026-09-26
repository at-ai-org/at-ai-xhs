import { createRouter, createWebHashHistory } from 'vue-router'
import Dashboard from '../views/Dashboard.vue'
import AccountManagement from '../views/AccountManagement.vue'
import MaterialManagement from '../views/MaterialManagement.vue'
import PublishCenter from '../views/PublishCenter.vue'
import PublishRecords from '../views/PublishRecords.vue'
import About from '../views/About.vue'
import XhsWorkbench from '../views/XhsWorkbench.vue'
import ModelSettings from '../views/ModelSettings.vue'

const routes = [
  {
    path: '/',
    name: 'Dashboard',
    component: Dashboard
  },
  {
    path: '/account-management',
    name: 'AccountManagement',
    component: AccountManagement
  },
  {
    path: '/material-management',
    name: 'MaterialManagement',
    component: MaterialManagement
  },
  {
    path: '/publish-center',
    name: 'PublishCenter',
    component: PublishCenter
  },
  {
    path: '/publish-records',
    name: 'PublishRecords',
    component: PublishRecords
  },
  {
    path: '/xhs-workbench',
    name: 'XhsWorkbench',
    component: XhsWorkbench
  },
  {
    path: '/model-settings',
    name: 'ModelSettings',
    component: ModelSettings
  },
  {
    path: '/about',
    name: 'About',
    component: About
  }
]

const router = createRouter({
  history: createWebHashHistory(),
  routes
})

export default router