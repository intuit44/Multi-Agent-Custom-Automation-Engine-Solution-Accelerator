import ChatInput from "@/coral/modules/ChatInput";
import { PlanChatProps } from "@/models";
import { Button, Menu, MenuTrigger, MenuPopover, MenuList, MenuItem, Divider } from "@fluentui/react-components";
import { Send } from "@/coral/imports/bundleicons";
import {
    Attach20Regular,
    Dismiss20Regular,
    ArrowDownload20Regular,
    Image20Regular,
    DocumentRegular,
    FolderRegular,
    MoreHorizontal20Regular
} from "@fluentui/react-icons";
import React, { useRef, useState } from "react";

interface SimplifiedPlanChatProps extends PlanChatProps {
    planData: any;
    input: string;
    setInput: (input: string) => void;
    submittingChatDisableInput: boolean;
    OnChatSubmit: (input: string) => void;
    waitingForPlan: boolean;
    attachedFiles?: Array<{ name: string; file_id: string }>;
    generatedFiles?: Array<{ file_id: string; filename: string; download_url: string }>;
    onFileSelect?: (file: File) => void;
    onRemoveFile?: (file_id: string) => void;
}

const PlanChatBody: React.FC<SimplifiedPlanChatProps> = ({
    planData,
    input,
    setInput,
    submittingChatDisableInput,
    OnChatSubmit,
    waitingForPlan,
    attachedFiles = [],
    generatedFiles = [],
    onFileSelect,
    onRemoveFile,
}) => {
    const isDisabled = submittingChatDisableInput || waitingForPlan;
    const placeholder = waitingForPlan
        ? "Waiting for plan..."
        : planData
            ? "Type your message here..."
            : "Describe your task...";
    const fileInputRef = useRef<HTMLInputElement>(null);
    const imageInputRef = useRef<HTMLInputElement>(null);
    const [menuOpen, setMenuOpen] = useState(false);

    const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
        const file = e.target.files?.[0];
        if (file && onFileSelect) {
            onFileSelect(file);
        }
        // Reset input
        if (fileInputRef.current) fileInputRef.current.value = "";
        setMenuOpen(false);
    };

    const handleImageChange = (e: React.ChangeEvent<HTMLInputElement>) => {
        const file = e.target.files?.[0];
        if (file && onFileSelect) {
            onFileSelect(file);
        }
        // Reset input
        if (imageInputRef.current) imageInputRef.current.value = "";
        setMenuOpen(false);
    };

    const getFileIcon = (fileName: string) => {
        const ext = fileName.split('.').pop()?.toLowerCase();
        if (['jpg', 'jpeg', 'png', 'gif', 'svg', 'webp'].includes(ext || '')) {
            return '🖼️';
        } else if (['pdf'].includes(ext || '')) {
            return '📄';
        } else if (['doc', 'docx'].includes(ext || '')) {
            return '📝';
        } else if (['xls', 'xlsx', 'csv'].includes(ext || '')) {
            return '📊';
        } else if (['zip', 'rar', '7z'].includes(ext || '')) {
            return '📦';
        }
        return '📎';
    };

    return (
        <div
            style={{
                bottom: 0,
                padding: '16px 24px',
                maxWidth: '800px',
                margin: '0 auto',
                marginBottom: '40px',
                width: '100%',
                boxSizing: 'border-box',
                zIndex: 10
            }}
        >
            {/* Hidden file inputs */}
            <input
                type="file"
                ref={fileInputRef}
                accept=".csv,.xlsx,.json,.txt,.pdf,.doc,.docx,.zip,.rar"
                onChange={handleFileChange}
                style={{ display: 'none' }}
            />
            <input
                type="file"
                ref={imageInputRef}
                accept="image/*"
                onChange={handleImageChange}
                style={{ display: 'none' }}
            />

            {/* Attached files display - Professional card style */}
            {attachedFiles.length > 0 && (
                <div style={{ marginBottom: '12px', display: 'flex', flexWrap: 'wrap', gap: '10px' }}>
                    {attachedFiles.map(f => (
                        <div
                            key={f.file_id}
                            style={{
                                display: 'flex',
                                alignItems: 'center',
                                gap: '10px',
                                padding: '10px 14px',
                                backgroundColor: 'var(--colorNeutralBackground2)',
                                border: '1px solid var(--colorNeutralStroke1)',
                                borderRadius: '8px',
                                fontSize: '13px',
                                fontWeight: 500,
                                boxShadow: '0 1px 2px rgba(0,0,0,0.05)',
                                transition: 'all 0.2s ease',
                                cursor: 'default',
                            }}
                            onMouseEnter={(e) => {
                                e.currentTarget.style.backgroundColor = 'var(--colorNeutralBackground3)';
                                e.currentTarget.style.boxShadow = '0 2px 4px rgba(0,0,0,0.08)';
                            }}
                            onMouseLeave={(e) => {
                                e.currentTarget.style.backgroundColor = 'var(--colorNeutralBackground2)';
                                e.currentTarget.style.boxShadow = '0 1px 2px rgba(0,0,0,0.05)';
                            }}
                        >
                            <span style={{ fontSize: '18px' }}>{getFileIcon(f.name)}</span>
                            <span style={{
                                color: 'var(--colorNeutralForeground1)',
                                maxWidth: '200px',
                                overflow: 'hidden',
                                textOverflow: 'ellipsis',
                                whiteSpace: 'nowrap',
                            }}>
                                {f.name}
                            </span>
                            <Button
                                appearance="transparent"
                                icon={<Dismiss20Regular />}
                                size="small"
                                onClick={() => onRemoveFile?.(f.file_id)}
                                style={{
                                    minWidth: 'auto',
                                    padding: '4px',
                                    height: '20px',
                                    width: '20px',
                                }}
                            />
                        </div>
                    ))}
                </div>
            )}

            {/* Generated files display - Professional download cards */}
            {generatedFiles.length > 0 && (
                <div style={{ marginBottom: '14px' }}>
                    <div style={{
                        fontSize: '12px',
                        fontWeight: 600,
                        marginBottom: '8px',
                        color: 'var(--colorNeutralForeground3)',
                        textTransform: 'uppercase',
                        letterSpacing: '0.5px',
                    }}>
                        Generated Files
                    </div>
                    <div style={{ display: 'flex', flexWrap: 'wrap', gap: '10px' }}>
                        {generatedFiles.map(f => (
                            <a
                                key={f.file_id}
                                href={f.download_url}
                                download={f.filename}
                                style={{
                                    display: 'flex',
                                    alignItems: 'center',
                                    gap: '10px',
                                    padding: '10px 14px',
                                    backgroundColor: 'var(--colorBrandBackground2)',
                                    borderRadius: '8px',
                                    fontSize: '13px',
                                    fontWeight: 500,
                                    textDecoration: 'none',
                                    color: 'var(--colorNeutralForeground1)',
                                    border: '1px solid var(--colorBrandStroke1)',
                                    boxShadow: '0 1px 2px rgba(0,0,0,0.05)',
                                    transition: 'all 0.2s ease',
                                }}
                                onMouseEnter={(e) => {
                                    e.currentTarget.style.backgroundColor = 'var(--colorBrandBackgroundHover)';
                                    e.currentTarget.style.boxShadow = '0 2px 4px rgba(0,0,0,0.1)';
                                    e.currentTarget.style.transform = 'translateY(-1px)';
                                }}
                                onMouseLeave={(e) => {
                                    e.currentTarget.style.backgroundColor = 'var(--colorBrandBackground2)';
                                    e.currentTarget.style.boxShadow = '0 1px 2px rgba(0,0,0,0.05)';
                                    e.currentTarget.style.transform = 'translateY(0)';
                                }}
                            >
                                <ArrowDownload20Regular style={{ color: 'var(--colorBrandForeground1)' }} />
                                <span style={{
                                    maxWidth: '200px',
                                    overflow: 'hidden',
                                    textOverflow: 'ellipsis',
                                    whiteSpace: 'nowrap',
                                }}>
                                    {f.filename}
                                </span>
                            </a>
                        ))}
                    </div>
                </div>
            )}

            <ChatInput
                value={input}
                onChange={setInput}
                onEnter={() => OnChatSubmit(input)}
                disabledChat={isDisabled}
                placeholder={placeholder}
                style={{
                    fontSize: '16px',
                    borderRadius: '12px',
                    width: '100%',
                    boxSizing: 'border-box',
                }}
            >
                {/* Professional Attach Menu */}
                <Menu open={menuOpen} onOpenChange={(_e, data) => setMenuOpen(data.open)}>
                    <MenuTrigger disableButtonEnhancement>
                        <Button
                            appearance="subtle"
                            icon={<Attach20Regular />}
                            disabled={isDisabled}
                            aria-label="Attach files and media"
                            style={{
                                height: '32px',
                                width: '32px',
                                borderRadius: '6px',
                                backgroundColor: 'transparent',
                                border: 'none',
                                color: isDisabled
                                    ? 'var(--colorNeutralForegroundDisabled)'
                                    : 'var(--colorNeutralForeground2)',
                                flexShrink: 0,
                            }}
                        />
                    </MenuTrigger>
                    <MenuPopover>
                        <MenuList>
                            <MenuItem
                                icon={<DocumentRegular />}
                                onClick={() => fileInputRef.current?.click()}
                            >
                                Add files and documents
                            </MenuItem>
                            <MenuItem
                                icon={<Image20Regular />}
                                onClick={() => imageInputRef.current?.click()}
                            >
                                Add photos and images
                            </MenuItem>
                            <Divider />
                            <MenuItem
                                icon={<FolderRegular />}
                                disabled
                            >
                                Recent files
                            </MenuItem>
                            <Divider />
                            <MenuItem
                                icon={<MoreHorizontal20Regular />}
                                disabled
                            >
                                More options
                            </MenuItem>
                        </MenuList>
                    </MenuPopover>
                </Menu>

                {/* Send Button */}
                <Button
                    appearance="subtle"
                    className="home-input-send-button"
                    onClick={() => OnChatSubmit(input)}
                    disabled={isDisabled || !input.trim()}
                    icon={<Send />}
                    aria-label="Send message"
                    style={{
                        height: '32px',
                        width: '32px',
                        borderRadius: '6px',
                        backgroundColor: (isDisabled || !input.trim())
                            ? 'transparent'
                            : 'var(--colorBrandBackground)',
                        border: 'none',
                        color: (isDisabled || !input.trim())
                            ? 'var(--colorNeutralForegroundDisabled)'
                            : 'var(--colorNeutralBackgroundStatic)',
                        flexShrink: 0,
                        transition: 'all 0.2s ease',
                    }}
                />
            </ChatInput>
        </div>
    );
}

export default PlanChatBody;
