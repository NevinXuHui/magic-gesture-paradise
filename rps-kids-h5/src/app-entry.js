import {createApp} from 'vue'
import ElButton from 'element-plus/es/components/button/index'
import ElSlider from 'element-plus/es/components/slider/index'
import 'element-plus/es/components/button/style/css'
import 'element-plus/es/components/slider/style/css'
import './style.css'
import App from './App.vue'

createApp(App).use(ElButton).use(ElSlider).mount('#app')
