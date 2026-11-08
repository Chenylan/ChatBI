import { memo, useEffect } from 'react';
import { Outlet, NavLink } from 'react-router-dom';
import { ConfigProvider, message } from 'antd';
import { ConstantProvider } from '@/hooks';
import * as constants from "@/utils/constants";
import { setMessage } from '@/utils';

// 顶部导航项样式：激活态高亮
const navItemClass = ({ isActive }: { isActive: boolean }) =>
  `px-16 py-6 text-[14px] rounded-[8px] transition-colors ${
    isActive
      ? 'text-[#4040ff] bg-[rgba(64,64,255,0.06)]'
      : 'text-[#666] hover:text-[#4040ff] hover:bg-[rgba(64,64,255,0.04)]'
  }`;

// Layout 组件：应用的主要布局结构
const Layout: GenieType.FC = memo(() => {
  const [messageApi, messageContent] = message.useMessage();

  useEffect(() => {
    // 初始化全局 message
    setMessage(messageApi);
  }, [messageApi]);

  return (
    <ConfigProvider theme={{ token: { colorPrimary: '#4040FFB2' } }}>
      {messageContent}
      <ConstantProvider value={constants}>
        <div className="h-full flex flex-col">
          {/* 顶部导航：页面选项（智能问数 / 数据表明细） */}
          <nav className="flex items-center gap-8 px-24 h-56 shrink-0 border-b border-[#E9E9F0] bg-white">
            <span className="mr-16 text-[16px] font-bold text-[#1b1b1b]">ChatBI</span>
            <NavLink to="/" end className={navItemClass}>智能问数</NavLink>
            <NavLink to="/schema" className={navItemClass}>数据表明细</NavLink>
            <NavLink to="/metrics" className={navItemClass}>指标口径</NavLink>
          </nav>
          <div className="flex-1 overflow-hidden">
            <Outlet />
          </div>
        </div>
      </ConstantProvider>
    </ConfigProvider>
  );
});

Layout.displayName = 'Layout';

export default Layout;
