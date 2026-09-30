import { createRouter, createWebHashHistory } from 'vue-router'
import Dashboard from '../views/Dashboard.vue'
import AccountManagement from '../views/AccountManagement.vue'
import MaterialManagement from '../views/MaterialManagement.vue'
import BackgroundMusic from '../views/BackgroundMusic.vue'
import PublishCenter from '../views/PublishCenter.vue'
import PublishRecords from '../views/PublishRecords.vue'
import About from '../views/About.vue'
import XhsWorkbench from '../views/XhsWorkbench.vue'
import ModelSettings from '../views/ModelSettings.vue'
import Login from '../views/Login.vue'
import { isLoggedIn } from '@/utils/session'

// `meta` 两个键是全站鉴权用的（at-ai-xhs 改造新增）：
//   public: true → 不需要登录也能进（目前只有登录页）
//   blank:  true → 不套侧边栏 / 顶栏那层布局（登录页要用整屏）
// 默认**不写就是需要登录**——以后新增页面忘了标注，也是被挡住而不是漏出去。
const routes = [
  {
    path: '/login',
    name: 'Login',
    component: Login,
    meta: { public: true, blank: true }
  },
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
    path: '/background-music',
    name: 'BackgroundMusic',
    component: BackgroundMusic
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
    // 「设置」页（原「模型设置」）。现在除了视觉模型，还放了账号会话续期等设置项，
    // 所以菜单名改成「设置」；老地址 /model-settings 留成 alias，
    // 免得别人收藏的链接、以及 README 里写过的地址失效。
    path: '/settings',
    alias: '/model-settings',
    name: 'Settings',
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

// 全局守卫：没登录一律赶回登录页。
//
// 这里读的是 localStorage（同步、零延迟），所以不会出现"先闪一下内容再跳走"。
// token 是不是**真的**还有效由后端说了算：失效时请求会拿到 401，
// 由 utils/request.js 统一清登录态并跳回这里。
router.beforeEach((to) => {
  const authed = isLoggedIn()

  if (to.meta.public) {
    // 已经登录的人不该再看到登录页（手敲 #/login 或点浏览器回退时）
    if (authed && to.path === '/login') return { path: '/' }
    return true
  }

  if (!authed) {
    return { path: '/login', query: to.fullPath === '/' ? {} : { redirect: to.fullPath } }
  }

  return true
})

export default router
