/**
 * 文件职责：前端应用入口，负责首屏数据加载、实时同步、视图装配与根节点挂载。
 */

function App() {
    const { state, dispatch } = useAppContext();
    const api = useApi();
    const mainContentRef = React.useRef(null);
    const isRestoringRef = React.useRef(false);

    const getScrollKey = React.useCallback(
        (view = state.currentView) => `astrbot_scroll_${view}`,
        [state.currentView]
    );

    const loadAll = React.useCallback(async () => {
        // 首次进入页面或手动全量刷新时，统一拉取首页所需的全部关键数据。
        dispatch({ type: 'SET_LOADING', payload: true });
        dispatch({ type: 'SET_ERROR', payload: '' });
        try {
            // 并发请求状态、会话、配置与任务，减少首屏等待时间。
            const [statusRes, sessionsRes, configRes, jobsRes] = await Promise.all([
                api.getStatus(),
                api.listSessions(),
                api.getConfig(),
                api.listJobs(),
            ]);
            dispatch({ type: 'SET_STATUS', payload: statusRes });
            dispatch({ type: 'SET_SESSIONS', payload: sessionsRes.sessions || [] });
            dispatch({ type: 'SET_CONFIG', payload: configRes || null });
            dispatch({ type: 'SET_JOBS', payload: jobsRes.jobs || [] });
        } catch (e) {
            // 将后端错误或网络错误统一透传到顶部错误卡片中展示。
            dispatch({ type: 'SET_ERROR', payload: e.message || '加载失败' });
        } finally {
            dispatch({ type: 'SET_LOADING', payload: false });
        }
    }, [api, dispatch]);

    const loadRealtime = React.useCallback(async () => {
        try {
            // 轻量轮询只更新变化频率最高的状态与任务列表，避免每秒都重载完整配置。
            const [statusRes, jobsRes] = await Promise.all([
                api.getStatus(),
                api.listJobs(),
            ]);
            dispatch({ type: 'SET_STATUS', payload: statusRes });
            dispatch({ type: 'SET_JOBS', payload: jobsRes.jobs || [] });
        } catch (e) {
            // 兜底轮询失败不打断主界面；实时信息短暂过期比整页报错更友好。
        }
    }, [api, dispatch]);

    React.useEffect(() => {
        // 若入口页仍在等待鉴权预检查，则挂起首次加载，等 auth-ready 事件触发后再开始。
        if (window.__PROACTIVE_AUTH_PENDING) {
            const onReady = () => loadAll();
            window.addEventListener('auth-ready', onReady);
            return () => window.removeEventListener('auth-ready', onReady);
        }
        loadAll();
    }, [loadAll]);

    useWebSocket(
        React.useCallback(
            (data) => {
                // WebSocket 推送只负责把增量/全量快照写回全局状态，不在这里做业务判断。
                if (data.status) dispatch({ type: 'SET_STATUS', payload: data.status });
                if (Array.isArray(data.jobs)) dispatch({ type: 'SET_JOBS', payload: data.jobs });
                if (Array.isArray(data.sessions)) {
                    // 后端可能返回字符串数组，也可能返回对象数组，这里统一标准化结构。
                    const mapped = data.sessions.map((s) =>
                        typeof s === 'string' ? { session: s, has_override: false } : s
                    );
                    dispatch({ type: 'SET_SESSIONS', payload: mapped });
                }
            },
            [dispatch]
        )
    );

    React.useEffect(() => {
        if (window.__PROACTIVE_AUTH_PENDING) return;

        let disposed = false;
        const tick = async () => {
            if (disposed) return;
            // 页面隐藏时暂停轮询，减少后台标签页的无意义请求。
            if (document.visibilityState === 'hidden') return;
            await loadRealtime();
        };

        // 每秒一次兜底轮询；即便 WebSocket 短暂断开，界面也能维持基本新鲜度。
        const timer = setInterval(tick, 1000);
        return () => {
            disposed = true;
            clearInterval(timer);
        };
    }, [loadRealtime]);

    React.useEffect(() => {
        if (window.__PROACTIVE_AUTH_PENDING) return;
        // 兼容旧版启动节点：一旦状态已返回，就主动隐藏遗留的 boot 元素。
        const boot = document.getElementById('boot');
        if (boot) boot.style.display = 'none';
    }, [state.status]);

    // 切换主视图时恢复对应滚动位置。
    React.useLayoutEffect(() => {
        const el = mainContentRef.current;
        if (!el) return;

        const key = getScrollKey();
        const savedPos = parseInt(SafeStorage.getItem(key) || '0', 10);

        if (savedPos > 0) {
            isRestoringRef.current = true;
            const applyRestore = () => {
                if (!mainContentRef.current) return;
                const maxScrollTop = Math.max(mainContentRef.current.scrollHeight - mainContentRef.current.clientHeight, 0);
                mainContentRef.current.scrollTop = Math.min(savedPos, maxScrollTop);
            };

            applyRestore();
            requestAnimationFrame(() => {
                if (!isRestoringRef.current) return;
                applyRestore();
            });

            const timer = window.setTimeout(() => {
                isRestoringRef.current = false;
            }, 320);

            return () => {
                window.clearTimeout(timer);
                isRestoringRef.current = false;
            };
        }

        el.scrollTop = 0;
        isRestoringRef.current = false;
    }, [state.currentView]);

    // 记录主内容区滚动位置，并在用户主动交互时终止恢复锁。
    React.useEffect(() => {
        const el = mainContentRef.current;
        if (!el) return;

        const stopRestoring = () => {
            isRestoringRef.current = false;
        };

        let timeout = 0;
        const handleScroll = () => {
            if (isRestoringRef.current) {
                const key = getScrollKey();
                const savedPos = parseInt(SafeStorage.getItem(key) || '0', 10);
                if (savedPos > 0 && Math.abs(el.scrollTop - savedPos) > 100) {
                    isRestoringRef.current = false;
                }
            }

            window.clearTimeout(timeout);
            timeout = window.setTimeout(() => {
                const key = getScrollKey();
                SafeStorage.setItem(key, String(el.scrollTop));
            }, 120);
        };

        el.addEventListener('scroll', handleScroll);
        el.addEventListener('wheel', stopRestoring, { passive: true });
        el.addEventListener('touchstart', stopRestoring, { passive: true });
        el.addEventListener('mousedown', stopRestoring);

        return () => {
            el.removeEventListener('scroll', handleScroll);
            el.removeEventListener('wheel', stopRestoring);
            el.removeEventListener('touchstart', stopRestoring);
            el.removeEventListener('mousedown', stopRestoring);
            window.clearTimeout(timeout);
        };
    }, [getScrollKey, state.currentView]);

    const renderView = () => {
        // 当前仅暴露三个主视图；未识别视图时回退到状态页，避免出现空白主区域。
        switch (state.currentView) {
            case 'status':
                return <StatusView onRefresh={loadAll} />;
            case 'config':
                return <ConfigView onRefresh={loadAll} />;
            case 'tasks':
                return <TasksView onRefresh={loadAll} />;
            case 'docs':
                return <MarkdownDocsView />;
            default:
                return <StatusView onRefresh={loadAll} />;
        }
    };

    // v1.3.0 起管理台固定为黑灰白暗色主题：主题类名由入口 HTML 预置，无切换逻辑。

    return (
        <div className="app">
            <Sidebar
                currentView={state.currentView}
                // 侧边栏只负责发出“切换视图”的意图，真正状态写入仍走 reducer。
                onChange={(view) => dispatch({ type: 'SET_VIEW', payload: view })}
            />
            <div className="main-wrapper">
                <Header currentView={state.currentView} />
                <div className="main-content" ref={mainContentRef}>
                    {/* 顶部错误条统一展示最近一次加载 / 操作失败的消息。 */}
                    {state.error ? <div className="card" style={{marginBottom: 0, color: '#ef9a9a', background: 'rgba(239, 154, 154, 0.08)'}}>错误：{state.error}</div> : null}
                    {renderView()}
                </div>
            </div>
            {/* 全局确认 / 提示对话框宿主：沙箱 iframe 中原生 alert/confirm 不可用。 */}
            <ProactiveDialogHost />
        </div>
    );
}

function ThemedAppShell() {
    const muiTheme = React.useMemo(() => {
        // 固定黑灰白暗色主题：直角、无渐变、中性灰配色，强调色仅为灰阶深浅。
        return MaterialUI.createTheme({
            palette: {
                mode: 'dark',
                primary: {
                    main: '#e8eaed',
                    contrastText: '#111214',
                },
                secondary: {
                    main: '#b8bcc2',
                },
                error: {
                    main: '#e57373',
                },
                warning: {
                    main: '#ffb74d',
                },
                success: {
                    main: '#81c784',
                },
                info: {
                    main: '#90a4ae',
                },
                background: {
                    default: '#111214',
                    paper: '#17181a',
                },
                text: {
                    primary: '#f2f3f5',
                    secondary: '#b8bcc2',
                },
                divider: '#2e3236',
            },
            shape: {
                borderRadius: 0,
            },
            typography: {
                fontFamily: '"Roboto", "Noto Sans SC", "Helvetica", "Arial", sans-serif',
            },
            components: {
                MuiPaper: {
                    styleOverrides: { root: { borderRadius: 0, backgroundImage: 'none' } },
                },
                MuiCard: {
                    styleOverrides: { root: { borderRadius: 0 } },
                },
                MuiButton: {
                    styleOverrides: { root: { borderRadius: 0, textTransform: 'none' } },
                },
                MuiOutlinedInput: {
                    styleOverrides: { root: { borderRadius: 0 } },
                },
                MuiChip: {
                    styleOverrides: { root: { borderRadius: 0 } },
                },
                MuiAccordion: {
                    styleOverrides: { root: { borderRadius: 0 } },
                },
                // 开关与整体主题一致改为直角矩形：暗色轨道 + 亮色滑块，开启时反色。
                MuiSwitch: {
                    styleOverrides: {
                        root: {
                            width: 52,
                            height: 32,
                            padding: 0,
                        },
                        switchBase: {
                            padding: '4px',
                            transitionDuration: '300ms',
                            '&.Mui-checked': {
                                transform: 'translateX(20px)',
                                color: '#111214',
                                '& + .MuiSwitch-track': {
                                    backgroundColor: '#e8eaed',
                                    opacity: 1,
                                    border: 0,
                                },
                            },
                        },
                        track: {
                            borderRadius: 0,
                            backgroundColor: '#2e3236',
                            opacity: 1,
                            boxSizing: 'border-box',
                            transition: 'background-color 300ms',
                        },
                        thumb: {
                            borderRadius: 0,
                            width: 24,
                            height: 24,
                            boxSizing: 'border-box',
                            backgroundColor: '#b8bcc2',
                            boxShadow: 'none',
                            transition: 'background-color 300ms',
                        },
                    },
                },
                MuiDialog: {
                    styleOverrides: { paper: { borderRadius: 0, backgroundImage: 'none' } },
                },
                MuiSnackbarContent: {
                    styleOverrides: { root: { borderRadius: 0 } },
                },
                MuiTooltip: {
                    styleOverrides: { tooltip: { borderRadius: 0 } },
                },
                MuiLinearProgress: {
                    styleOverrides: { root: { borderRadius: 0 } },
                },
            },
        });
    }, []);

    return (
        <MaterialUI.ThemeProvider theme={muiTheme}>
            <App />
        </MaterialUI.ThemeProvider>
    );
}

function AuthWrapper() {
    // ready 初值根据启动页鉴权状态决定，避免无鉴权场景下多等一次事件。
    const [ready, setReady] = React.useState(() => !window.__PROACTIVE_AUTH_PENDING);

    React.useEffect(() => {
        if (ready) return;
        // 等待入口页完成鉴权预检查后，再真正挂载 React 应用。
        const onReady = () => setReady(true);
        window.addEventListener('auth-ready', onReady);
        return () => window.removeEventListener('auth-ready', onReady);
    }, [ready]);

    if (!ready) return null;

    return (
        <AppProvider>
            <ThemedAppShell />
        </AppProvider>
    );
}

if (!window.__PROACTIVE_WEBUI_INITIALIZED) {
    // 防止脚本重复执行时反复 createRoot，避免 React 在同一节点重复挂载。
    window.__PROACTIVE_WEBUI_INITIALIZED = true;
    const root = ReactDOM.createRoot(document.getElementById('root'));
    root.render(<AuthWrapper />);
}

