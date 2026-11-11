import { memo, useEffect, useState } from 'react';
import { Table, Button, Modal, Form, Input, Select, Switch, Tag, Popconfirm, message, Space, Typography } from 'antd';
import type { TableColumnType } from 'antd';
import { agentApi } from '@/services/agent';

// 指标口径数据结构（与后端 metrics/store.py 的 list_metrics 返回一致）
interface Metric {
  canonical: string;
  aliases: string[];
  formula: string;
  sql_expr: string;
  group: string;
  default: boolean;
  builtin: boolean;
  updated_at: number;
}

// 表单字段（新增/编辑共用）
interface MetricFormValues {
  canonical: string;
  aliases: string[];
  formula: string;
  sql_expr: string;
  group: string;
  default: boolean;
}

// 「指标口径」页面：查看/新增/编辑/删除指标口径字典
const Metrics: GenieType.FC<Record<string, never>> = memo(() => {
  const [loading, setLoading] = useState(true);
  const [list, setList] = useState<Metric[]>([]);
  const [modalOpen, setModalOpen] = useState(false);
  const [editing, setEditing] = useState<Metric | null>(null);
  const [saving, setSaving] = useState(false);
  const [form] = Form.useForm<MetricFormValues>();

  // 拉取全部口径
  const load = () => {
    setLoading(true);
    agentApi
      .getMetrics()
      .then((res: any) => {
        const data = res.data || res;
        setList(Array.isArray(data) ? data : []);
      })
      .catch((err) => {
        console.error('获取指标口径失败', err);
        message.error('获取指标口径失败');
      })
      .finally(() => setLoading(false));
  };

  useEffect(load, []);

  // 打开「新增」弹窗
  const openCreate = () => {
    setEditing(null);
    form.resetFields();
    form.setFieldsValue({ aliases: [], default: false });
    setModalOpen(true);
  };

  // 打开「编辑」弹窗
  const openEdit = (m: Metric) => {
    setEditing(m);
    form.setFieldsValue({
      canonical: m.canonical,
      aliases: m.aliases || [],
      formula: m.formula,
      sql_expr: m.sql_expr,
      group: m.group,
      default: m.default,
    });
    setModalOpen(true);
  };

  // 保存（新增或更新）
  const handleSave = async () => {
    try {
      const values = await form.validateFields();
      setSaving(true);
      await agentApi.saveMetric(values);
      message.success(editing ? '已更新口径' : '已新增口径');
      setModalOpen(false);
      load();
    } catch (err: any) {
      // 表单校验失败时 err 带 errorFields，直接忽略不弹错误提示
      if (err?.errorFields) return;
      message.error(err?.response?.data?.detail || '保存失败');
    } finally {
      setSaving(false);
    }
  };

  // 删除
  const handleDelete = async (canonical: string) => {
    try {
      await agentApi.deleteMetric(canonical);
      message.success('已删除口径');
      load();
    } catch (err: any) {
      message.error(err?.response?.data?.detail || '删除失败');
    }
  };

  const columns: TableColumnType<Metric>[] = [
    {
      title: '口径名',
      dataIndex: 'canonical',
      key: 'canonical',
      render: (v: string) => <Typography.Text strong>{v}</Typography.Text>,
    },
    {
      title: '别名',
      dataIndex: 'aliases',
      key: 'aliases',
      render: (v: string[]) => (v?.length ? v.map((a) => <Tag key={a}>{a}</Tag>) : '-'),
    },
    { title: '公式', dataIndex: 'formula', key: 'formula' },
    {
      title: 'SQL 表达式',
      dataIndex: 'sql_expr',
      key: 'sql_expr',
      render: (v: string) => <Typography.Text code>{v}</Typography.Text>,
    },
    {
      title: '触发词',
      dataIndex: 'group',
      key: 'group',
      render: (v: string) => (v ? <Tag color="purple">{v}</Tag> : '-'),
    },
    {
      title: '默认',
      dataIndex: 'default',
      key: 'default',
      width: 70,
      render: (v: boolean) => (v ? <Tag color="blue">默认</Tag> : null),
    },
    {
      title: '来源',
      dataIndex: 'builtin',
      key: 'builtin',
      width: 80,
      render: (v: boolean) => (v ? <Tag>内置</Tag> : <Tag color="green">自定义</Tag>),
    },
    {
      title: '操作',
      key: 'action',
      width: 140,
      render: (_, record) => (
        <Space>
          <Button size="small" type="link" onClick={() => openEdit(record)}>
            编辑
          </Button>
          <Popconfirm title="确定删除该口径？" onConfirm={() => handleDelete(record.canonical)}>
            <Button size="small" type="link" danger>
              删除
            </Button>
          </Popconfirm>
        </Space>
      ),
    },
  ];

  return (
    <div className="h-full overflow-auto p-24">
      <div className="mb-16 flex items-center justify-between">
        <div>
          <Typography.Title level={4} style={{ margin: 0 }}>
            指标口径
          </Typography.Title>
          <Typography.Text type="secondary">
            自定义指标口径后，会实时注入模型提示词，影响歧义澄清与 SQL 计算
          </Typography.Text>
        </div>
        <Button type="primary" onClick={openCreate}>
          新增口径
        </Button>
      </div>

      <Table<Metric>
        rowKey="canonical"
        loading={loading}
        dataSource={list}
        columns={columns}
        pagination={false}
      />

      <Modal
        title={editing ? '编辑口径' : '新增口径'}
        open={modalOpen}
        onOk={handleSave}
        onCancel={() => setModalOpen(false)}
        confirmLoading={saving}
        forceRender
      >
        <Form form={form} layout="vertical">
          <Form.Item
            name="canonical"
            label="口径名"
            rules={[{ required: true, message: '请输入口径名' }]}
          >
            <Input placeholder="如：客单价" disabled={!!editing} />
          </Form.Item>
          <Form.Item name="aliases" label="别名">
            <Select mode="tags" placeholder="输入别名后回车，如：客均收入" tokenSeparators={[',']} />
          </Form.Item>
          <Form.Item name="formula" label="公式 / 口径说明">
            <Input placeholder="如：销售收入 ÷ 订单数" />
          </Form.Item>
          <Form.Item name="sql_expr" label="SQL 表达式">
            <Input placeholder="如：SUM(销售收入) / COUNT(DISTINCT 订单号)" />
          </Form.Item>
          <Form.Item name="group" label="触发词（可选）">
            <Input placeholder="如：客单。留空则不计入歧义检测分组" />
          </Form.Item>
          <Form.Item name="default" label="默认口径" valuePropName="checked">
            <Switch />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
});

Metrics.displayName = 'Metrics';

export default Metrics;
