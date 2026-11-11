import { useEffect, useState, memo } from 'react';
import { Table, Spin, Typography, Tag } from 'antd';
import type { TableColumnType } from 'antd';
import { agentApi } from '@/services/agent';

// 字段明细
interface ColumnSchema {
  name: string;
  type: string;
  nullable: boolean;
  primaryKey: boolean;
}

// 表明细
interface TableSchema {
  tableName: string;
  rowCount: number | null;
  columns: ColumnSchema[];
}

// 「数据表明细」页面：展示当前数据源下所有表的结构（表名 / 字段 / 类型 / 行数）
const DataSchema: GenieType.FC<Record<string, never>> = memo(() => {
  const [loading, setLoading] = useState(true);
  const [database, setDatabase] = useState('');
  const [tables, setTables] = useState<TableSchema[]>([]);

  useEffect(() => {
    agentApi
      .getSchema()
      .then((res: any) => {
        const data = res.data || res; // 兼容 axios 响应对象与直接返回数据两种形态
        setDatabase(data.database || '');
        setTables(Array.isArray(data.tables) ? data.tables : []);
      })
      .catch((err) => {
        console.error('获取数据表明细失败', err);
        setTables([]);
      })
      .finally(() => setLoading(false));
  }, []);

  // 字段明细表格（展开某张表后显示）
  const columnColumns: TableColumnType<ColumnSchema>[] = [
    { title: '字段名', dataIndex: 'name', key: 'name' },
    { title: '类型', dataIndex: 'type', key: 'type' },
    {
      title: '主键',
      dataIndex: 'primaryKey',
      key: 'primaryKey',
      width: 90,
      render: (v: boolean) => (v ? <Tag color="blue">主键</Tag> : null),
    },
    {
      title: '可空',
      dataIndex: 'nullable',
      key: 'nullable',
      width: 80,
      render: (v: boolean) => (v ? '是' : '否'),
    },
  ];

  // 表列表列
  const tableColumns: TableColumnType<TableSchema>[] = [
    { title: '表名', dataIndex: 'tableName', key: 'tableName' },
    {
      title: '字段数',
      dataIndex: 'columns',
      key: 'colCount',
      width: 100,
      render: (cols: ColumnSchema[]) => cols?.length ?? 0,
    },
    {
      title: '行数',
      dataIndex: 'rowCount',
      key: 'rowCount',
      width: 120,
      render: (v: number | null) => (v == null ? '-' : v.toLocaleString()),
    },
  ];

  return (
    <div className="h-full overflow-auto p-24">
      <div className="mb-16">
        <Typography.Title level={4} style={{ margin: 0 }}>
          数据表明细{database ? ` · ${database}` : ''}
        </Typography.Title>
        <Typography.Text type="secondary">当前数据源下共 {tables.length} 张表，点击行展开查看字段明细</Typography.Text>
      </div>

      <Spin spinning={loading}>
        <Table<TableSchema>
          rowKey="tableName"
          dataSource={tables}
          columns={tableColumns}
          expandable={{
            expandedRowRender: (record) => (
              <Table<ColumnSchema>
                rowKey="name"
                dataSource={record.columns}
                columns={columnColumns}
                pagination={false}
                size="small"
              />
            ),
          }}
          pagination={false}
        />
      </Spin>
    </div>
  );
});

DataSchema.displayName = 'DataSchema';

export default DataSchema;
