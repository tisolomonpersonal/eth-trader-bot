# ETH Trader Mobile - Interface Design

## Overview
A native-feeling Android app that wraps the ETH Trader web chat interface (https://tbot.zeabur.app/chat) in a seamless WebView, designed to feel like a first-party mobile app.

## Screen List

1. **Splash Screen** - App launch with branding
2. **Chat Screen** - Main WebView displaying the trading bot chat interface
3. **Settings Screen** - App preferences and info (optional, accessible via menu)

## Primary Content and Functionality

### Chat Screen (Main)
- **Content**: Full-screen WebView rendering https://tbot.zeabur.app/chat
- **Functionality**:
  - Display the web chat interface without visible browser chrome
  - Handle user input and messaging within the web app
  - Maintain session state across app lifecycle
  - Smooth scrolling and touch interactions
  - Status bar integration (light/dark mode aware)
  - Safe area handling for notch/home indicator

### Settings Screen (Optional)
- **Content**: App info, version, clear cache option
- **Functionality**: Manage app preferences, view about info

## Key User Flows

1. **App Launch Flow**:
   - Splash screen displays (1-2 seconds)
   - Chat screen loads with WebView
   - Web app initializes and displays chat interface

2. **Chat Interaction Flow**:
   - User types message in web chat
   - WebView handles input and displays responses
   - Smooth scrolling within chat
   - User can interact with all web app features

3. **Navigation Flow**:
   - Tab bar provides access to Chat (primary) and Settings (secondary)
   - Back button behavior handled by WebView (back in chat history)

## Color Choices

- **Primary**: #0a7ea4 (Professional blue, matches trading theme)
- **Background**: #ffffff (light) / #151718 (dark)
- **Surface**: #f5f5f5 (light) / #1e2022 (dark)
- **Foreground**: #11181C (light) / #ECEDEE (dark)
- **Border**: #E5E7EB (light) / #334155 (dark)

## Native UI Polish

- Status bar styled to match app theme
- WebView edges rounded slightly for modern feel
- Smooth transitions between screens
- Haptic feedback on tab selection
- Safe area properly handled for all devices
- Loading indicator while web content loads
- Error handling with retry option
