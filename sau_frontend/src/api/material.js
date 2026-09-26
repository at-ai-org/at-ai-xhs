import { http } from '@/utils/request'

// 素材管理API
export const materialApi = {
  // 获取所有素材
  getAllMaterials: () => {
    return http.get('/getFiles')
  },
  
  // 上传素材
  uploadMaterial: (formData, onUploadProgress) => {
    // 使用http.upload方法，它已经配置了正确的Content-Type
    return http.upload('/uploadSave', formData, onUploadProgress)
  },
  
  // 删除素材
  deleteMaterial: (id) => {
    return http.get(`/deleteFile?id=${id}`)
  },
  
  // 下载素材
  downloadMaterial: (filePath) => {
    // 后端没有 /download 路由，下载和预览走同一个 /getFile（会带 Content-Disposition 之外的原始字节）
    const filename = String(filePath || '').split('/').pop()
    return `${import.meta.env.VITE_API_BASE_URL || 'http://localhost:5409'}/getFile?filename=${encodeURIComponent(filename)}`
  },
  
  // 获取素材预览URL
  getMaterialPreviewUrl: (filename) => {
    // 必须编码：素材名可能含空格、中文、emoji（AI 重命名后会），
    // 直接拼进 query 会被浏览器截断或解析错，表现就是"预览一直加载中"。
    return `${import.meta.env.VITE_API_BASE_URL || 'http://localhost:5409'}/getFile?filename=${encodeURIComponent(filename || '')}`
  }
}