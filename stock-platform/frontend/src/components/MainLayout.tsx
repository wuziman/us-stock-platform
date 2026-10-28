import React, { useState } from 'react';
import { Layout, Menu, Drawer, Button } from 'antd';
import {
  HomeOutlined,
  LineChartOutlined,
  FundOutlined,
  RobotOutlined,
  MenuOutlined
} from '@ant-design/icons';
import { useNavigate, useLocation } from 'react-router-dom';
import { colors } from '../theme/tokens';

const { Header, Sider, Content } = Layout;

interface MainLayoutProps {
  children: React.ReactNode;
}

const MainLayout: React.FC<MainLayoutProps> = ({ children }) => {
  const navigate = useNavigate();
  const location = useLocation();
  const [navOpen, setNavOpen] = useState(false);

  const menuItems = [
    {
      key: '/',
      icon: <HomeOutlined />,
      label: '首页',
    },
    {
      key: '/analysis',
      icon: <LineChartOutlined />,
      label: '股票分析',
    },
    {
      key: '/portfolio',
      icon: <FundOutlined />,
      label: '持仓管理',
    },
    {
      key: '/ai-pick',
      icon: <RobotOutlined />,
      label: '精选标的',
    },
  ];

  return (
    <Layout style={{ minHeight: '100vh' }}>
      {/* 桌面端侧边栏；<992px 自动收起。trigger={null} 移除 antd 默认的零宽触发条
          （span[role=img]，键盘无法操作），移动端导航统一走下方抽屉 */}
      <Sider breakpoint="lg" collapsedWidth="0" trigger={null} style={{ background: '#fff' }}>
        <div style={{
          height: 64,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          borderBottom: `1px solid ${colors.border}`
        }}>
          <h2 style={{ margin: 0, color: colors.primary }}>📈 量化平台</h2>
        </div>
        <Menu
          mode="inline"
          selectedKeys={[location.pathname]}
          items={menuItems}
          onClick={({ key }) => navigate(key)}
          style={{ borderRight: 0 }}
        />
      </Sider>
      <Layout>
        <Header style={{
          background: '#fff',
          padding: '0 24px',
          borderBottom: `1px solid ${colors.border}`,
          display: 'flex',
          alignItems: 'center'
        }}>
          <Button
            className="mobile-nav-btn"
            type="text"
            aria-label="打开导航菜单"
            icon={<MenuOutlined />}
            onClick={() => setNavOpen(true)}
          />
          <h3 style={{ margin: 0 }}>A股 & 美股量化分析系统</h3>
        </Header>
        <Content style={{ margin: '24px 16px', padding: 24, background: '#fff', borderRadius: 8 }}>
          {children}
        </Content>
      </Layout>

      {/* 移动端抽屉导航（按钮可Tab聚焦，抽屉支持Esc关闭） */}
      <Drawer
        title="📈 量化平台"
        open={navOpen}
        onClose={() => setNavOpen(false)}
        width={220}
        styles={{ body: { padding: 0 } }}
      >
        <Menu
          mode="inline"
          selectedKeys={[location.pathname]}
          items={menuItems}
          onClick={({ key }) => { navigate(key); setNavOpen(false); }}
          style={{ borderRight: 0 }}
        />
      </Drawer>
    </Layout>
  );
};

export default MainLayout;
