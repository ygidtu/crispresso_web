import { createApp } from 'vue'
import * as naive from 'naive-ui'
import App from './App.vue'

// naive-ui 样式由运行时 StyleManager 动态注入，无需静态 CSS 引入

const app = createApp(App)
app.use(naive)
app.mount('#app')
