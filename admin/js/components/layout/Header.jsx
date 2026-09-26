(() => {
/**
 * 文件职责：顶部栏组件，负责标题展示与时钟显示。
 */

 const { Box, Typography } = MaterialUI;
 const { useState, useEffect } = React;

function RealTimeClock({ timeZone }) {
    const [timeStr, setTimeStr] = useState('');

    useEffect(() => {
        const updateTime = () => {
            // 头部时钟始终使用统一的格式化工具，确保与状态页、任务页时间显示风格一致。
            setTimeStr(formatDateTime(new Date(), timeZone || 'Asia/Shanghai', {
                includeYear: true,
                includeSeconds: true,
            }));
        };

        updateTime();
        // 每秒刷新一次时钟文本；组件卸载时清理定时器，避免后台泄漏。
        const timer = setInterval(updateTime, 1000);
        return () => clearInterval(timer);
    }, [timeZone]);

    // 初始尚未生成时间字符串时先不渲染，避免短暂出现占位空壳。
    if (!timeStr) return null;

    return (
        <div className="header-clock-chip">
            <span className="header-clock-label">当前时间 🕒</span>
            <span className="header-clock-value">{timeStr}</span>
        </div>
    );
}

function Header({ currentView }) {
    const { state } = useAppContext();
    const { config } = state;
    // 若配置中未单独指定展示时区，则默认按插件主要使用场景的东八区展示。
    const displayTimezone = config?.displayTimezone || 'Asia/Shanghai';

    // 视图 key 到标题文案的映射集中维护，避免 JSX 中散落条件判断。
    const viewTitles = {
        status: '运行状态',
        tasks: '任务管理',
        docs: '文档浏览',
        config: '配置管理',
    };

    return (
        <>
            <div className="top-bar">
                <Typography variant="h5" sx={{
                    fontWeight: 800,
                    color: 'text.primary',
                    letterSpacing: '-0.5px'
                }}>
                    {viewTitles[currentView] || viewTitles.status}
                </Typography>

                <Box sx={{ display: 'flex', alignItems: 'center', gap: 2, flexWrap: 'wrap', justifyContent: 'flex-end' }}>
                    <RealTimeClock timeZone={displayTimezone} />
                </Box>
            </div>
        </>
    );
}

// 暴露到全局，供入口应用直接渲染 Header。
window.Header = Header;
})();

