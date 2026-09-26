(() => {
/**
 * 文件职责：侧边导航组件，负责视图切换与 logo 品牌展示。
 */

 const { Box, Typography } = MaterialUI;

function Sidebar({ currentView, onChange }) {
    const [logoSrc, setLogoSrc] = React.useState('');

    React.useEffect(() => {
        let cancelled = false;

        // 独立页与插件页沙箱的静态资源解析方式不同，分别选择可靠的加载途径：
        // 沙箱 iframe 中相对路径一律 404，必须经 bridge 的 asset 接口取 data URL；
        // 独立页部署在 admin/ 层级，直接引用插件根目录的 logo 即可。
        async function resolveLogo() {
            if (window.__PROACTIVE_EMBEDDED__ && window.HttpUtil) {
                try {
                    const result = await window.HttpUtil.get('asset/logo.png');
                    if (
                        !cancelled &&
                        result &&
                        result.kind === 'dataurl' &&
                        result.content
                    ) {
                        setLogoSrc(result.content);
                    }
                } catch (e) {
                    // logo 加载失败不阻塞主界面，仅隐藏图片占位。
                }
                return;
            }
            if (!cancelled) setLogoSrc('../logo.png');
        }

        resolveLogo();
        return () => {
            cancelled = true;
        };
    }, []);

    // 当前管理端开放的主导航项集中定义在这里，便于后续扩展新视图。
    const menus = [
        { key: 'status', label: '运行状态', icon: '📊' },
        { key: 'tasks', label: '任务管理', icon: '📋' },
        { key: 'docs', label: '文档浏览', icon: '📚' },
        { key: 'config', label: '配置管理', icon: '⚙️' },
    ];

    return (
        <div className="sidebar">
            <div className="sidebar-header">
                {logoSrc ? (
                    <img src={logoSrc} alt="Logo" className="sidebar-logo-img" />
                ) : null}
                <div>
                    <Typography variant="h6" sx={{ fontWeight: 800, lineHeight: 1.2, letterSpacing: '-0.5px' }}>
                        主动消息
                    </Typography>
                    <Typography variant="caption" sx={{ opacity: 0.6, display: 'block', mt: 0.5 }}>
                        Admin Console
                    </Typography>
                </div>
            </div>

            <Box sx={{ flex: 1, mt: 4 }}>
                {menus.map((item) => (
                    <div
                        key={item.key}
                        className={`nav-item ${currentView === item.key ? 'active' : ''}`}
                        // 导航只上抛目标视图 key，具体状态更新由父组件统一处理。
                        onClick={() => onChange(item.key)}
                    >
                        <span style={{ fontSize: '18px' }}>{item.icon}</span>
                        <Typography variant="body2" sx={{ fontWeight: 500 }}>
                            {item.label}
                        </Typography>
                    </div>
                ))}
            </Box>
        </div>
    );
}

// 暴露到全局，供入口应用直接使用侧边栏组件。
window.Sidebar = Sidebar;
})();

