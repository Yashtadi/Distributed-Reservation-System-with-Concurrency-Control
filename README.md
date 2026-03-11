# Distributed-Reservation-System-with-Concurrency-Control
# Problem Definition:
Design and implement a distributed train reservation system using TCP socket programming that allows multiple clients to reserve seats concurrently while ensuring atomic and consistent booking of shared resources. The system must prevent double booking through concurrency control mechanisms, support secure communication using SSL/TLS, and handle multiple client requests simultaneously.

# Objectives:
- Implement a network based reservation system using TCP socket
- Allow multiple clients to connect to a server simultaneously
- Design a custom communication protocol between client and server
- Prevent Double booking using concurrency control mechanisms
- Secure all communications using SSL/TLS encryption
- Evaluate system performance under concurrent client load

# Architecture 
- We are choosing client-server architecture because for this project many clients interact with a single service provider =

# System Components
1. Client Application
     - allows user to interact with the reservation server
     - Connect to server
     - send reservation requests
     - recieve responses
     - display booking results
  
2. Reservation server
    - responsible for managing reservations
    - accept client connections
    - process requests
    - enforce concurrency control
    - maintain seat availability
    - send responses to clients

# Internal server modules
1. Protocol Handler
   - Interpret client messages
   - Determine which actions to perform
  
2. Lock Manager
   - This module prevents race condition
   - Mechanism:
       a. Accquire seat lock
       b. Check availability
       c. Reserve seat
       d. Release lock

3. Seat Database
   Stores seat status
   
5. Thread Manager
   Handles multiple concurrent clients
   - Server accepts connection
   - Server creates new thread
   - Thread handles client requests

# Communication Flow
  - Client connects to server
  - Client sends request
  - Server processes request
  - Server sends response

# Custom Protocol Design
1. Client request format
     COMMAND | DATA
   example: BOOK | 12

2. Server Response Format
     STATUS | MESSAGE
   example: SUCCES | Seat 12 booked

# Overall System workflow
- Client connects to server
- Server authenticates connection
- Client sends request
- Server parses request
- Server checks seat availability
- Server applies concurrency control
- Server updates database
- Server sends response

  
