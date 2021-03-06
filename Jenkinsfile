pipeline {
    agent any
    options {
        disableConcurrentBuilds()
    }

    stages {

        stage("Build Images") {
            // Builds images that are required for tests
            steps {
                dir("__riptide_lib") {
                    git url: 'https://github.com/Parakoopa/riptide-lib.git'
                    sh "docker build -t riptide_docker_tox test_assets/riptide-docker-tox"
                }
            }
        }

        stage("Tests") {
            steps {
                lock('riptide-integration-tests') {
                    // Tox has issues with the venv directories for some reasons, so we just reset it each time
                    sh "rm .tox -rf || true"
                    sh '''#!/bin/bash
                        docker run \
                            -v /var/run/docker.sock:/var/run/docker.sock \
                            -e USER=$(id -u) \
                            -e DOCKER_GROUP=$(cut -d: -f3 < <(getent group docker)) \
                            -v "/tmp:/tmp" \
                            -v "$(pwd):$(pwd)" \
                            --network host \
                            --workdir $(pwd) \
                            riptide_docker_tox \
                            tox
                    '''
                }
            }
        }

        stage('Build') {
            steps {
                // Setup virtual env
                sh "rm -rf .venv || true"
                sh "virtualenv .venv"

                // Run build
                sh "rm -rf dist build || true"
                sh '''. .venv/bin/activate
                      pip3 install -r requirements.txt
                      python3 setup.py bdist_wheel
                '''
            }
            post {
                always {
                    archiveArtifacts allowEmptyArchive: true, artifacts: 'dist/*whl', fingerprint: true
                }
            }
        }

        stage('Deploy to PyPI') {
            when {
                branch "release"
            }
            environment {
                TWINE    = credentials('parakoopa-twine-username-password')
            }
            steps {
                sh 'twine upload -u "$TWINE_USR" -p "$TWINE_PSW" dist/*'
            }
        }

    }

    post {
        always {
            junit 'test_reports/**/*.xml'
            sh "rm test_reports -rf || true"
        }
    }

}