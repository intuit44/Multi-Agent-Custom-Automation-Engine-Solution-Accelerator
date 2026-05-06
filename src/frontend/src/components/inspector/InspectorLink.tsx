import React, { useCallback, useEffect, useState } from "react";
import {
    Button,
    Tooltip,
    Badge,
    Popover,
    PopoverTrigger,
    PopoverSurface,
    Text,
    Spinner,
} from "@fluentui/react-components";
import {
    BranchFork20Regular,
    Open20Regular,
    CheckmarkCircle20Regular,
    DismissCircle20Regular,
} from "@fluentui/react-icons";
import { apiClient } from "../../api/apiClient";

interface InspectorStatus {
    running: boolean;
    proxy_url: string;
    ui_url: string;
    ui_link: string;
    auth_token?: string;
    message?: string;
}

/**
 * InspectorLink — Icon button that opens MCP Inspector UI.
 *
 * Placed in the ContentToolbar, it shows:
 * - Green badge when Inspector is running
 * - Red badge when Inspector is not running
 * - Popover with status details and "Open Inspector" button
 *
 * The Inspector UI runs on its own port (16274) and is opened
 * in a new browser tab — no embedding in the MACAE frontend.
 */
const InspectorLink: React.FC = () => {
    const [status, setStatus] = useState<InspectorStatus | null>(null);
    const [loading, setLoading] = useState(true);

    const checkStatus = useCallback(async () => {
        try {
            const response = await apiClient.get("/v4/mcp/inspector/status");
            setStatus(response.data);
        } catch {
            setStatus({
                running: false,
                proxy_url: "http://localhost:16277",
                ui_url: "http://localhost:16274",
                ui_link:
                    "http://localhost:16274/?transportType=streamable-http&url=http%3A%2F%2Flocalhost%3A9000%2Fmcp",
                message: "Inspector status unavailable",
            });
        } finally {
            setLoading(false);
        }
    }, []);

    useEffect(() => {
        checkStatus();
    }, [checkStatus]);

    const handleOpenInspector = () => {
        // Use ui_link from backend which already includes MCP_PROXY_AUTH_TOKEN
        const url = status?.ui_link ||
            "http://localhost:16274/?transportType=streamable-http&url=http%3A%2F%2Flocalhost%3A9000%2Fmcp";
        window.open(url, "_blank", "noopener,noreferrer");
    };

    if (loading) {
        return (
            <Tooltip content="Checking MCP Inspector..." relationship="label">
                <Button
                    appearance="subtle"
                    icon={<Spinner size="tiny" />}
                    size="small"
                />
            </Tooltip>
        );
    }

    return (
        <Popover withArrow>
            <PopoverTrigger disableButtonEnhancement>
                <Tooltip content="MCP Inspector" relationship="label">
                    <Button
                        appearance="subtle"
                        size="small"
                        icon={
                            <Badge
                                size="extra-small"
                                color={status?.running ? "success" : "danger"}
                                shape="circular"
                                style={{
                                    position: "absolute",
                                    top: 2,
                                    right: 2,
                                    width: 8,
                                    height: 8,
                                    minWidth: 8,
                                }}
                            >
                                {""}
                            </Badge>
                        }
                        style={{ position: "relative" }}
                    >
                        <BranchFork20Regular />
                    </Button>
                </Tooltip>
            </PopoverTrigger>
            <PopoverSurface
                style={{
                    padding: "16px",
                    display: "flex",
                    flexDirection: "column",
                    gap: "12px",
                    minWidth: "260px",
                }}
            >
                <div
                    style={{
                        display: "flex",
                        alignItems: "center",
                        gap: "8px",
                    }}
                >
                    {status?.running ? (
                        <CheckmarkCircle20Regular
                            style={{ color: "var(--colorStatusSuccessForeground1)" }}
                        />
                    ) : (
                        <DismissCircle20Regular
                            style={{ color: "var(--colorStatusDangerForeground1)" }}
                        />
                    )}
                    <Text weight="semibold">
                        MCP Inspector {status?.running ? "Running" : "Offline"}
                    </Text>
                </div>

                {!status?.running && (
                    <Text size={200} style={{ color: "var(--colorNeutralForeground3)" }}>
                        Start Inspector with:
                        <br />
                        <code
                            style={{
                                fontSize: "11px",
                                background: "var(--colorNeutralBackground3)",
                                padding: "2px 6px",
                                borderRadius: "4px",
                            }}
                        >
                            ./scripts/start_inspector.sh
                        </code>
                    </Text>
                )}

                <Button
                    appearance="primary"
                    size="small"
                    icon={<Open20Regular />}
                    onClick={handleOpenInspector}
                    style={{ alignSelf: "stretch" }}
                >
                    Open Inspector UI
                </Button>

                <Text
                    size={100}
                    style={{
                        color: "var(--colorNeutralForeground4)",
                        textAlign: "center",
                    }}
                >
                    Test & debug MCP servers, tools, and resources
                </Text>
            </PopoverSurface>
        </Popover>
    );
};

export default InspectorLink;
