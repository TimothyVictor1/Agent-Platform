import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import HomePage from "./page";

describe("HomePage placeholder", () => {
  it("shows the app name and the configured API URL", () => {
    render(<HomePage />);
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Agent Platform");
    expect(screen.getByTestId("api-url")).toHaveTextContent("http");
  });
});
