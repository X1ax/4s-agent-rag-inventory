import { createApp } from 'vue'
import { ElButton, ElTag, ElInput, ElSelect, ElOption, ElTable, ElTableColumn, ElPagination, ElDialog, ElForm, ElFormItem, ElInputNumber, ElSwitch, ElLoading } from 'element-plus'
import 'element-plus/dist/index.css'
import './app.css'
import App from './App.vue'

const app = createApp(App)
for (const component of [ElButton, ElTag, ElInput, ElSelect, ElOption, ElTable, ElTableColumn, ElPagination, ElDialog, ElForm, ElFormItem, ElInputNumber, ElSwitch, ElLoading]) app.use(component)
app.mount('#app')
