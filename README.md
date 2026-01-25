# 🧱 Open XLIFF Translator

[![Build Status](https://img.shields.io/github/actions/workflow/status/BondIT-ApS/open-xliff-translator/docker-publish.yml?branch=main&style=for-the-badge)](https://github.com/BondIT-ApS/open-xliff-translator/actions)
[![License](https://img.shields.io/github/license/BondIT-ApS/open-xliff-translator?style=for-the-badge)](LICENSE)
[![Repo Size](https://img.shields.io/github/repo-size/BondIT-ApS/open-xliff-translator?style=for-the-badge)](https://github.com/BondIT-ApS/open-xliff-translator)
[![Made in Denmark](https://img.shields.io/badge/made%20in-Denmark%20🇩🇰-red?style=for-the-badge)](https://bondit.dk)

[![Docker Hub](https://img.shields.io/badge/Docker%20Hub-open--xliff--translator--frontend-blue?logo=docker&style=for-the-badge)](https://hub.docker.com/r/maboni82/open-xliff-translator-frontend)
[![Docker Pulls](https://img.shields.io/docker/pulls/maboni82/open-xliff-translator-frontend?style=for-the-badge)](https://hub.docker.com/r/maboni82/open-xliff-translator-frontend)

[![Docker Hub](https://img.shields.io/badge/Docker%20Hub-open--xliff--translator--backend-blue?logo=docker&style=for-the-badge)](https://hub.docker.com/r/maboni82/open-xliff-translator-backend)
[![Docker Pulls](https://img.shields.io/docker/pulls/maboni82/open-xliff-translator-backend?style=for-the-badge)](https://hub.docker.com/r/maboni82/open-xliff-translator-backend)

## 🔤 Building Translation Solutions, One Brick at a Time

Welcome to Open XLIFF Translator - where we do for translation workflows what LEGO did for building: make them structured, reliable, and surprisingly enjoyable! 

Just like assembling a LEGO masterpiece, we've crafted a solution that transforms complex XLIFF translation workflows into something elegant and straightforward. This Dockerized web-based translation tool uses Flask, Nginx, and LibreTranslate to create a seamless translation experience. Upload your XLIFF files, watch the magic happen, and download your translated content - all with the precision and reliability you'd expect from a well-engineered LEGO creation.

## 🚀 Features - The Building Blocks

- **📄 XLIFF Translation Magic** – Automatically translate `.xlf` files to different languages, like having a universal translator in your LEGO toolkit
- **🎨 Clean Web Interface** – Upload, process, and download files through an intuitive web interface, as satisfying as that perfect LEGO brick click
- **🐳 Dockerized Deployment** – Quick, containerized setup that works everywhere, following clear instructions like a LEGO manual
- **🔓 LibreTranslate Integration** – Powered by open-source LibreTranslate engine, because the best building blocks should be accessible to everyone
- **⚡ Real-time Processing** – Watch your files transform in real-time, like seeing your LEGO creation come to life
- **📱 Responsive Design** – Works beautifully on desktop and mobile, adapting like modular LEGO pieces

## 🧱 Getting Started - The Foundation Pieces

### Prerequisites - Tools You'll Need

- [Docker](https://www.docker.com/get-started) - Your primary building tool
- [Docker Compose](https://docs.docker.com/compose/install/) - For connecting the pieces

### Installation - Assembly Instructions

1. **📦 Clone the repository**:
    ```bash
    git clone https://github.com/BondIT-ApS/open-xliff-translator.git
    cd open-xliff-translator
    ```

2. **🚀 Assemble the Solution**:
    ```bash
    docker-compose up -d --build
    ```
    Just like that final satisfying "click" when LEGO pieces connect, your containers are now running!

3. **🎯 Access Your Translator**:
    - **Translation Interface**: http://localhost:5173
    - **Backend API**: http://localhost:5002

## 🎮 Usage - Playing with Your Creation

1. Open **http://localhost:5173** in your browser
2. Upload your `.xlf` file using the clean interface
3. Watch as the file is automatically processed and translated
4. Download your translated file with a single click

It's like building with LEGO - simple steps that create something amazing!

### 🐳 Docker Hub Building Sets

Our pre-built Docker images are ready for your collection:

- **Backend Set**: [open-xliff-translator-backend](https://hub.docker.com/r/maboni82/open-xliff-translator-backend)
- **Frontend Set**: [open-xliff-translator-frontend](https://hub.docker.com/r/maboni82/open-xliff-translator-frontend)

## 🧰 Project Architecture - The Building Design

Just like a well-designed LEGO set, this solution consists of several key components:

1. **Frontend (React/Vite)** - The user-friendly interface pieces that make translation accessible
2. **Backend API (Flask)** - The foundation pieces that handle file processing and translation
3. **LibreTranslate Engine** - The specialized pieces that perform the actual translation magic
4. **Docker Containers** - The instruction manual that makes assembly a breeze

## 👷‍♂️ Contributing - Join Our Building Team

Contributions are welcome! Feel free to open an issue or submit a pull request. Like any good LEGO enthusiast, we believe more builders create better creations.

1. Fork the repository (like borrowing a few bricks)
2. Create your feature branch (`git checkout -b feature/amazing-feature`)
3. Commit your changes (`git commit -m 'Add some amazing feature'`)
4. Push to the branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request (show us your creation!)

## 📄 License - The Building Rules

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.
Like LEGO, you're free to rebuild and reimagine as you see fit!

---

## 🏢 About BondIT ApS

This project is maintained by [BondIT ApS](https://bondit.dk), a Danish IT consultancy that builds digital solutions one brick at a time. Just like our fellow Danish company LEGO, we believe in building things methodically, with precision and a touch of playfulness. Because the best solutions, like the best LEGO creations, are both functional AND fun!

**Made with ❤️, ☕, and 🧱 by BondIT ApS**
