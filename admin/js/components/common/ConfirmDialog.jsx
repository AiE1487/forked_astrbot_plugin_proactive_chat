/**
 * 文件职责：页面内确认 / 提示对话框，替代沙箱 iframe 中被拦截的原生 alert / confirm。
 *
 * AstrBot 插件 Pages 在沙箱 iframe（无 allow-modals）中加载，原生
 * alert / confirm 弹窗会被浏览器静默拦截，因此管理台统一改用本组件：
 * - window.ProactiveDialog.confirm({title, message, ...}) → Promise<boolean>
 * - window.ProactiveDialog.alert(message, title) → Promise<void>
 * 必须在应用根部渲染一个 <ProactiveDialogHost /> 承载弹窗。
 */

const { useState, useEffect } = React;

function ProactiveDialogHost() {
    const [dialog, setDialog] = useState(null);

    useEffect(() => {
        // 注册全局弹窗通道：window.ProactiveDialog 的请求都会投递到这里。
        window.__proactiveDialogSink = (options) =>
            new Promise((resolve) => setDialog({ options, resolve }));
        return () => {
            window.__proactiveDialogSink = null;
        };
    }, []);

    const close = (result) => {
        setDialog((prev) => {
            if (prev && typeof prev.resolve === 'function') prev.resolve(result);
            return null;
        });
    };

    if (!dialog) return null;
    const options = dialog.options || {};
    const isAlert = !!options.alert;

    return (
        <MaterialUI.Dialog
            open
            onClose={() => close(isAlert ? undefined : false)}
            PaperProps={{
                sx: {
                    borderRadius: 0,
                    bgcolor: 'background.paper',
                    border: '1px solid',
                    borderColor: 'divider',
                    backgroundImage: 'none',
                    minWidth: 320,
                    maxWidth: 480,
                },
            }}
        >
            <MaterialUI.DialogTitle sx={{ color: 'text.primary', py: 1.5, px: 2 }}>
                {options.title || (isAlert ? '提示' : '确认操作')}
            </MaterialUI.DialogTitle>
            <MaterialUI.DialogContent sx={{ py: 0.5, px: 2 }}>
                <MaterialUI.DialogContentText
                    component="div"
                    sx={{ whiteSpace: 'pre-line', color: 'text.secondary', m: 0 }}
                >
                    {options.message}
                </MaterialUI.DialogContentText>
            </MaterialUI.DialogContent>
            <MaterialUI.DialogActions sx={{ px: 2, py: 1.5 }}>
                {!isAlert && (
                    <MaterialUI.Button onClick={() => close(false)} color="inherit">
                        {options.cancelText || '取消'}
                    </MaterialUI.Button>
                )}
                <MaterialUI.Button
                    onClick={() => close(isAlert ? undefined : true)}
                    variant="contained"
                    color={options.danger ? 'error' : 'primary'}
                    disableElevation
                >
                    {options.confirmText || (isAlert ? '知道了' : '确定')}
                </MaterialUI.Button>
            </MaterialUI.DialogActions>
        </MaterialUI.Dialog>
    );
}

window.ProactiveDialog = {
    /** 弹出确认框，resolve(true/false) 表示用户选择。 */
    confirm: function (options) {
        if (typeof window.__proactiveDialogSink !== 'function') {
            // Host 尚未挂载时兜底返回 false，避免调用方流程卡死。
            console.warn('[主动消息] 确认对话框通道未就绪，已按“取消”处理喵');
            return Promise.resolve(false);
        }
        return window.__proactiveDialogSink(Object.assign({ alert: false }, options || {}));
    },

    /** 弹出提示框（仅一个确定按钮）。 */
    alert: function (message, title) {
        if (typeof window.__proactiveDialogSink !== 'function') {
            console.warn('[主动消息] 提示对话框通道未就绪，消息已降级到控制台喵');
            console.warn(String(message == null ? '' : message));
            return Promise.resolve();
        }
        return window.__proactiveDialogSink({
            alert: true,
            title: title || '提示',
            message: String(message == null ? '' : message),
        });
    },
};

// 多文件 UMD / Babel 直接挂载方案：通过 window 暴露给 app.jsx 渲染。
window.ProactiveDialogHost = ProactiveDialogHost;
