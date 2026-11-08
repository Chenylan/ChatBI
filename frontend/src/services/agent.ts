import api from "./index";

export const agentApi = {
  // ChatBI API
  chatQuery: (data: { query: string; session_id?: string; request_id?: string; model?: string }) => 
    api.post(`/api/chat/query`, data),
  healthCheck: () => api.get(`/api/chat/health`),
  allModels: () => api.get(`/api/chat/models`),
  // 获取当前数据源下所有表的结构明细（表名 + 字段 + 类型 + 行数）
  getSchema: () => api.get(`/api/chat/schema`),
  // 指标口径字典：列表 / 保存（新增或更新）/ 删除
  getMetrics: () => api.get(`/api/metrics`),
  saveMetric: (data: {
    canonical: string; aliases: string[]; formula: string; sql_expr: string; group: string; default: boolean;
  }) => api.post(`/api/metrics`, data),
  deleteMetric: (canonical: string) => api.delete(`/api/metrics/${encodeURIComponent(canonical)}`),
};
