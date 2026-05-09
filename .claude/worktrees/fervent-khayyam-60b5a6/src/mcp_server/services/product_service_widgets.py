"""
Product MCP tools service with Widget support (FastMCP 3.1+, MCP Protocol 2025-11-25).

This service demonstrates:
- createUIResource for widget components
- registerAppResource for UI registration
- structuredContent with _meta.ui.resourceUri
"""

from core.factory import Domain, MCPToolBase


class ProductServiceWithWidgets(MCPToolBase):
    """Product tools with interactive widget support."""

    def __init__(self):
        super().__init__(Domain.PRODUCT)

    def register_tools(self, mcp) -> None:
        """Register Product tools with widgets using FastMCP 3.1+ APIs."""

        # Register UI resources using @resource decorator applied manually
        mcp.resource(
            uri="ui://product-card/{product_id}",
            name="Product Card Widget",
            description="Interactive product card widget with details and actions",
            mime_type="text/html",
        )(self._product_card_widget)

        mcp.resource(
            uri="ui://product-comparison",
            name="Product Comparison Widget",
            description="Interactive comparison table for phone plans",
            mime_type="text/html",
        )(self._product_comparison_widget)

        # Register tools
        mcp.tool(tags={self.domain.value})(self.get_product_info)
        mcp.tool(tags={self.domain.value})(self.compare_products)

    async def _product_card_widget(self, product_id: str) -> str:
        """UI Resource: Interactive product card widget. Returns HTML string."""
        return f"""
        <div class="product-card" style="border: 1px solid #e0e0e0; border-radius: 8px; padding: 16px; max-width: 400px; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;">
            <h3 style="margin: 0 0 12px 0; color: #1a1a1a;">📱 Product Details</h3>
            <div class="product-info">
                <p><strong>Product ID:</strong> {product_id}</p>
                <p><strong>Name:</strong> Premium Phone Plan</p>
                <p><strong>Price:</strong> <span style="font-size: 1.2em; color: #2563eb;">$70/month</span></p>
                <p><strong>Data:</strong> Unlimited</p>
                <p><strong>Status:</strong> <span style="color: #10b981; font-weight: 600;">✅ In Stock</span></p>
            </div>
            <button style="margin-top: 12px; padding: 8px 16px; background: #2563eb; color: white; border: none; border-radius: 6px; cursor: pointer; font-size: 14px;"
                    onclick="alert('Added ' + '{product_id}' + ' to cart!')">
                Add to Cart
            </button>
        </div>
        """

    async def _product_comparison_widget(self) -> str:
        """UI Resource: Interactive product comparison table. Returns HTML string."""
        return """
        <div class="product-comparison" style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;">
            <h3 style="margin: 0 0 12px 0; color: #1a1a1a;">📊 Compare Phone Plans</h3>
            <table style="width: 100%; border-collapse: collapse; text-align: left;">
                <thead>
                    <tr style="background: #f3f4f6;">
                        <th style="padding: 10px; border-bottom: 2px solid #e5e7eb;">Feature</th>
                        <th style="padding: 10px; border-bottom: 2px solid #e5e7eb;">Basic</th>
                        <th style="padding: 10px; border-bottom: 2px solid #e5e7eb;">Standard</th>
                        <th style="padding: 10px; border-bottom: 2px solid #e5e7eb; color: #2563eb; font-weight: 700;">Premium ⭐</th>
                    </tr>
                </thead>
                <tbody>
                    <tr><td style="padding: 10px; border-bottom: 1px solid #f3f4f6;">Price</td><td style="padding: 10px; border-bottom: 1px solid #f3f4f6;">$25/mo</td><td style="padding: 10px; border-bottom: 1px solid #f3f4f6;">$45/mo</td><td style="padding: 10px; border-bottom: 1px solid #f3f4f6; font-weight: 600;">$70/mo</td></tr>
                    <tr><td style="padding: 10px; border-bottom: 1px solid #f3f4f6;">Data</td><td style="padding: 10px; border-bottom: 1px solid #f3f4f6;">5 GB</td><td style="padding: 10px; border-bottom: 1px solid #f3f4f6;">15 GB</td><td style="padding: 10px; border-bottom: 1px solid #f3f4f6; font-weight: 600;">Unlimited</td></tr>
                    <tr><td style="padding: 10px; border-bottom: 1px solid #f3f4f6;">5G</td><td style="padding: 10px; border-bottom: 1px solid #f3f4f6;">❌</td><td style="padding: 10px; border-bottom: 1px solid #f3f4f6;">✅</td><td style="padding: 10px; border-bottom: 1px solid #f3f4f6;">✅</td></tr>
                    <tr><td style="padding: 10px;">Hotspot</td><td style="padding: 10px;">❌</td><td style="padding: 10px;">5 GB</td><td style="padding: 10px; font-weight: 600;">Unlimited</td></tr>
                </tbody>
            </table>
        </div>
        """

    async def get_product_info(self, product_id: str = "premium-plan") -> dict:
        """Get information about phone plans with interactive widget."""
        markdown = (
            f"## Product Info for {product_id}\n\nPrice: $70/month\nData: Unlimited"
        )

        return {
            "content": [{"type": "text", "text": markdown}],
            "structuredContent": {"id": product_id, "price": 70},
            "_meta": {
                "ui": {
                    "resourceUri": f"ui://product-card/{product_id}",
                    "fallback": "markdown",
                }
            },
        }

    async def compare_products(self) -> dict:
        """Compare available phone plans side-by-side."""
        markdown = "## Compare Plans\n\n| Plan | Price |\n|------|-------|\n| A | $25 |\n| B | $45 |\n| C | $70 |"

        return {
            "content": [{"type": "text", "text": markdown}],
            "structuredContent": {"plans": 3},
            "_meta": {
                "ui": {"resourceUri": "ui://product-comparison", "fallback": "markdown"}
            },
        }

    @property
    def tool_count(self) -> int:
        """Return the number of tools provided by this service."""
        return 2
