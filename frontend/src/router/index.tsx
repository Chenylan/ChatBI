import React, { Suspense } from 'react';
import { createBrowserRouter, Navigate } from 'react-router-dom';
import Layout from '@/layout/index';
import { Loading } from '@/components';

// 使用常量存储路由路径
const ROUTES = {
  HOME: '/',
  SCHEMA: '/schema',
  METRICS: '/metrics',
  NOT_FOUND: '*',
};

// 使用 React.lazy 懒加载组件
const Home = React.lazy(() => import('@/pages/Home'));
const DataSchema = React.lazy(() => import('@/pages/DataSchema'));
const Metrics = React.lazy(() => import('@/pages/Metrics'));
const NotFound = React.lazy(() => import('@/components/NotFound'));

// 创建路由配置
const router = createBrowserRouter([
  {
    path: ROUTES.HOME,
    element: <Layout />,
    children: [
      {
        index: true,
        element: (
          <Suspense fallback={<Loading loading={true} className="h-full"/>}>
            <Home />
          </Suspense>
        ),
      },
      {
        path: ROUTES.SCHEMA,
        element: (
          <Suspense fallback={<Loading loading={true} className="h-full"/>}>
            <DataSchema />
          </Suspense>
        ),
      },
      {
        path: ROUTES.METRICS,
        element: (
          <Suspense fallback={<Loading loading={true} className="h-full"/>}>
            <Metrics />
          </Suspense>
        ),
      },
      {
        path: ROUTES.NOT_FOUND,
        element: (
          <Suspense fallback={<Loading loading={true} className="h-full"/>}>
            <NotFound />
          </Suspense>
        ),
      },
    ],
  },
  // 重定向所有未匹配的路由到 404 页面
  {
    path: '*',
    element: <Navigate to={ROUTES.NOT_FOUND} replace />,
  },
]);

export default router;
